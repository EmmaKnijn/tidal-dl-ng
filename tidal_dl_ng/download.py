import base64
import json
import os
import random
import shutil
import tempfile
import time
from collections.abc import Callable
from logging import Logger
from uuid import uuid4

import ffmpeg
import m3u8
import requests
from mpegdash.parser import MPEGDASHParser
from requests.exceptions import HTTPError
from rich.progress import Progress, TaskID
from tidalapi import Album, Mix, Playlist, Session, Track, UserPlaylist, Video

from tidal_dl_ng.config import Settings
from tidal_dl_ng.constants import REQUESTS_TIMEOUT_SEC, CoverDimensions, MediaType, SkipExisting, StreamManifestMimeType
from tidal_dl_ng.helper.decryption import decrypt_file, decrypt_security_token
from tidal_dl_ng.helper.exceptions import MediaMissing, MediaUnknown, UnknownManifestFormat
from tidal_dl_ng.helper.path import check_file_exists, format_path_media, path_file_sanitize
from tidal_dl_ng.helper.tidal import name_builder_item
from tidal_dl_ng.helper.wrapper import WrapperLogger
from tidal_dl_ng.metadata import Metadata
from tidal_dl_ng.model.gui_data import ProgressBars
from tidal_dl_ng.model.tidal import StreamManifest


# TODO: Set appropriate client string and use it for video download.
# https://github.com/globocom/m3u8#using-different-http-clients
class RequestsClient:
    def download(
        self, uri: str, timeout: int = REQUESTS_TIMEOUT_SEC, headers: dict | None = None, verify_ssl: bool = True
    ):
        if not headers:
            headers = {}

        o = requests.get(uri, timeout=timeout, headers=headers)

        return o.text, o.url


class Download:
    settings: Settings = None
    session: Session = None
    skip_existing: SkipExisting = False

    def __init__(self, session: Session, skip_existing: SkipExisting = SkipExisting.Disabled):
        self.settings = Settings()
        self.session = session
        self.skip_existing = skip_existing

    def _download(
        self,
        fn_logger: Callable,
        media: Track | Video,
        progress: Progress,
        progress_gui: ProgressBars,
        stream_manifest: StreamManifest,
        path_file: str,
    ):
        media_name: str = name_builder_item(media)

        # Set the correct progress output channel.
        if progress_gui is None:
            progress_stdout: bool = True
        else:
            progress_stdout: bool = False
            # Send signal to GUI with media name
            progress_gui.item_name.emit(media_name)

        try:
            # Compute total iterations for progress
            urls_count: int = len(stream_manifest.urls)

            if urls_count > 1:
                progress_total: int = urls_count
                block_size: int | None = None
            else:
                # Compute progress iterations based on the file size.
                r = requests.get(stream_manifest.urls[0], stream=True, timeout=REQUESTS_TIMEOUT_SEC)

                r.raise_for_status()

                # Get file size and compute progress steps
                total_size_in_bytes: int = int(r.headers.get("content-length", 0))
                block_size: int | None = 4096
                progress_total: float = total_size_in_bytes / block_size

            # Create progress Task
            p_task: TaskID = progress.add_task(
                f"[blue]Item '{media_name[:30]}'",
                total=progress_total,
                visible=progress_stdout,
            )

            # Write content to file until progress is finished.
            while not progress.tasks[p_task].finished:
                with open(path_file, "wb") as f:
                    for url in stream_manifest.urls:
                        # Create the request object with stream=True, so the content won't be loaded into memory at once.
                        r = requests.get(url, stream=True, timeout=REQUESTS_TIMEOUT_SEC)

                        r.raise_for_status()

                        # Write the content to disk. If `chunk_size` is set to `None` the whole file will be written at once.
                        for data in r.iter_content(chunk_size=block_size):
                            f.write(data)
                            # Advance progress bar.
                            progress.advance(p_task)

                            # To send the progress to the GUI, we need to emit the percentage.
                            if not progress_stdout:
                                progress_gui.item.emit(progress.tasks[p_task].percentage)
        except HTTPError as e:
            # TODO: Handle Exception...
            fn_logger(e)

        # Check if file is encrypted.
        needs_decryption = self.is_encrypted(stream_manifest.encryption_type)

        if needs_decryption:
            key, nonce = decrypt_security_token(stream_manifest.encryption_key)
            tmp_path_file_decrypted = path_file + "_decrypted"
            decrypt_file(path_file, tmp_path_file_decrypted, key, nonce)
        else:
            tmp_path_file_decrypted = path_file

        # Write metadata to file.
        if not isinstance(media, Video):
            self.metadata_write(media, tmp_path_file_decrypted)

        return tmp_path_file_decrypted

    def instantiate_media(
        self,
        session: Session,
        media_type: type[MediaType.TRACK, MediaType.VIDEO, MediaType.ALBUM, MediaType.PLAYLIST, MediaType.MIX],
        id_media: str,
    ) -> Track | Video:
        if media_type == MediaType.TRACK:
            media = Track(session, id_media)
        elif media_type == MediaType.VIDEO:
            media = Video(session, id_media)
        elif media_type == MediaType.ALBUM:
            media = Album(self.session, id_media)
        elif media_type == MediaType.PLAYLIST:
            media = Playlist(self.session, id_media)
        elif media_type == MediaType.MIX:
            media = Mix(self.session, id_media)
        else:
            raise MediaUnknown

        return media

    def item(
        self,
        path_base: str,
        file_template: str,
        fn_logger: Callable,
        media: Track | Video = None,
        media_id: str = None,
        media_type: MediaType = None,
        video_download: bool = True,
        progress_gui: ProgressBars = None,
        progress: Progress = None,
    ) -> (bool, str):
        # If no media instance is provided, we need to create the media instance.
        if media_id and media_type:
            media = self.instantiate_media(self.session, media_type, media_id)
        elif not media:
            raise MediaMissing

        # If video download is not allowed end here
        if not video_download:
            fn_logger.info(
                f"Video downloads are deactivated (see settings). Skipping video: {name_builder_item(media)}"
            )

            return False, ""

        # Create file name and path
        file_name_relative = format_path_media(file_template, media)
        path_file = os.path.abspath(os.path.normpath(os.path.join(path_base, file_name_relative)))

        # Populate StreamManifest for further download.
        if isinstance(media, Track):
            stream = media.stream()
            manifest: str = stream.manifest
            mime_type: str = stream.manifest_mime_type
        else:
            manifest: str = media.get_url()
            mime_type: str = StreamManifestMimeType.VIDEO.value

        stream_manifest = self.stream_manifest_parse(manifest, mime_type)

        # Sanitize final path_file to fit into OS boundaries.
        path_file = path_file_sanitize(path_file + stream_manifest.file_extension, adapt=True)

        # Compute if and how downloads need to be skipped.
        if self.skip_existing.value:
            extension_ignore = self.skip_existing == SkipExisting.ExtensionIgnore
            # TODO: Check if extension is already in `path_file` or not.
            download_skip = check_file_exists(path_file, extension_ignore=extension_ignore)
        else:
            download_skip = False

        if not download_skip:
            # Create a temp directory and file.
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_path_dir:
                tmp_path_file = os.path.join(tmp_path_dir, str(uuid4()) + stream_manifest.file_extension)
                # Download media.
                tmp_path_file = self._download(fn_logger, media, progress, progress_gui, stream_manifest, tmp_path_file)

                if isinstance(media, Video) and self.settings.data.video_convert_mp4:
                    # TODO: Make optional.
                    # Convert `*.ts` file to `*.mp4` using ffmpeg
                    tmp_path_file = self._video_convert(tmp_path_file)
                    path_file = os.path.splitext(path_file)[0] + ".mp4"

                # Move final file to the configured destination directory.
                os.makedirs(os.path.dirname(path_file), exist_ok=True)
                shutil.move(tmp_path_file, path_file)
        else:
            fn_logger.debug(f"Download skipped, since file exists: '{path_file}'")

        return not download_skip, path_file

    def cover_url(self, sid: str, dimension: CoverDimensions = CoverDimensions.Px320):
        if sid is None:
            return ""

        return f"https://resources.tidal.com/images/{sid.replace('-', '/')}/{dimension.value}.jpg"

    def metadata_write(self, track: Track, path_file: str):
        result: bool = False
        release_date: str = (
            track.album.release_date.strftime("%Y-%m-%d") if track.album and track.album.release_date else ""
        )
        copy_right: str = track.copyright if hasattr(track, "copyright") and track.copyright else ""
        isrc: str = track.isrc if hasattr(track, "isrc") and track.isrc else ""
        lyrics: str = ""

        if self.settings.data.lyrics_save:
            # Try to retrieve lyrics.
            try:
                lyrics: str = track.lyrics().subtitles if hasattr(track, "lyrics") else ""
            except HTTPError:
                # TODO: Implement proper logging.
                print(f"Could not retrieve lyrics for `{name_builder_item(track)}`.")

        # TODO: Check if it is possible to pass "None" values.
        m: Metadata = Metadata(
            path_file=path_file,
            lyrics=lyrics,
            copy_right=copy_right,
            title=track.name,
            artists=[artist.name for artist in track.artists],
            album=track.album.name if track.album else "",
            tracknumber=track.track_num,
            date=release_date,
            isrc=isrc,
            albumartist=name_builder_item(track),
            totaltrack=track.album.num_tracks if track.album and track.album.num_tracks else 1,
            totaldisc=track.album.num_volumes if track.album and track.album.num_volumes else 1,
            discnumber=track.volume_num if track.volume_num else 1,
            url_cover=(
                self.cover_url(track.album.cover, self.settings.data.metadata_cover_dimension) if track.album else ""
            ),
        )

        m.save()

        result = True

        return result

    def items(
        self,
        path_base: str,
        fn_logger: Logger | WrapperLogger,
        media_id: str = None,
        media_type: MediaType = None,
        file_template: str = None,
        media: Album | Playlist | UserPlaylist | Mix = None,
        video_download: bool = False,
        progress_gui: ProgressBars = None,
        progress: Progress = None,
        download_delay: bool = True,
    ):
        # If no media instance is provided, we need to create the media instance.
        if media_id and media_type:
            media = self.instantiate_media(self.session, media_type, media_id)
        elif not media:
            raise MediaMissing

        # Create file name and path
        file_name_relative = format_path_media(file_template, media)

        # TODO: Extend with pagination support: Iterate through `items` and `tracks`until len(returned list) == 0
        # Get the items and name of the list.
        if isinstance(media, Mix):
            items = media.items()
            list_media_name = media.title[:30]
        elif video_download:
            items = media.items(limit=100)
            list_media_name = media.name[:30]
        else:
            items = media.tracks(limit=999)
            list_media_name = media.name[:30]

        # Determine where to redirect the progress information.
        if progress_gui is None:
            progress_stdout: bool = True
        else:
            progress_stdout: bool = False

        # Create the list progress task.
        p_task1: TaskID = progress.add_task(
            f"[green]List '{list_media_name}'", total=len(items), visible=progress_stdout
        )

        # Iterate through list items
        while not progress.finished:
            for media in items:
                # TODO: Handle return value of `track` method.
                # Download the item.
                status_download, result_path_file = self.item(
                    path_base=path_base,
                    file_template=file_name_relative,
                    media=media,
                    progress_gui=progress_gui,
                    progress=progress,
                    fn_logger=fn_logger,
                )

                # Advance progress bar.
                progress.advance(p_task1)

                if not progress_stdout:
                    progress_gui.list_item.emit(progress.tasks[p_task1].percentage)

                # If a file was downloaded and the download delay is enabled, wait until the next download.
                if download_delay and status_download:
                    time_sleep: float = round(random.SystemRandom().uniform(2, 5), 1)

                    # TODO: Fix logging. Is not displayed in debug window.
                    fn_logger.debug(f"Next download will start in {time_sleep} seconds.")
                    time.sleep(time_sleep)

    def is_encrypted(self, encryption_type: str) -> bool:
        result = encryption_type != "NONE"

        return result

    def get_file_extension(self, stream_url: str, stream_codec: str) -> str:
        if ".flac" in stream_url:
            result: str = ".flac"
        elif ".mp4" in stream_url:
            # TODO: Need to investigate, what the correct extension is.
            # if "ac4" in stream_codec or "mha1" in stream_codec:
            #     result = ".mp4"
            # elif "flac" in stream_codec:
            #     result = ".flac"
            # else:
            #     result = ".m4a"
            result: str = ".mp4"
        elif ".ts" in stream_url:
            result: str = ".ts"
        else:
            result: str = ".m4a"

        return result

    def _video_convert(self, path_file: str) -> str:
        path_file_out = os.path.splitext(path_file)[0] + ".mp4"
        result, _ = ffmpeg.input(path_file).output(path_file_out, map=0, c="copy").run()

        return path_file_out

    def stream_manifest_parse(self, manifest: str, mime_type: str) -> StreamManifest:
        if mime_type == StreamManifestMimeType.MPD.value:
            # Stream Manifest is base64 encoded.
            manifest_parsed: str = base64.b64decode(manifest).decode("utf-8")
            mpd = MPEGDASHParser.parse(manifest_parsed)
            codecs: str = mpd.periods[0].adaptation_sets[0].representations[0].codecs
            mime_type: str = mpd.periods[0].adaptation_sets[0].mime_type
            # TODO: Handle encryption key. But I have never seen an encrypted file so far.
            encryption_type: str = "NONE"
            encryption_key: str | None = None
            # .initialization + the very first of .media; See https://developers.broadpeak.io/docs/foundations-dash
            segments_count = 1 + 1

            for s in mpd.periods[0].adaptation_sets[0].representations[0].segment_templates[0].segment_timelines[0].Ss:
                segments_count += s.r if s.r else 1

            # Populate segment urls.
            segment_template = mpd.periods[0].adaptation_sets[0].representations[0].segment_templates[0]
            stream_urls: list[str] = []

            for index in range(segments_count):
                stream_urls.append(segment_template.media.replace("$Number$", str(index)))

        elif mime_type == StreamManifestMimeType.BTS.value:
            # Stream Manifest is base64 encoded.
            manifest_parsed: str = base64.b64decode(manifest).decode("utf-8")
            # JSON string to object.
            stream_manifest = json.loads(manifest_parsed)
            # TODO: Handle more than one download URL
            stream_urls: str = stream_manifest["urls"]
            codecs: str = stream_manifest["codecs"]
            mime_type: str = stream_manifest["mimeType"]
            encryption_type: str = stream_manifest["encryptionType"]
            encryption_key: str | None = (
                stream_manifest["encryptionKey"] if self.is_encrypted(encryption_type) else None
            )
        elif mime_type == StreamManifestMimeType.VIDEO.value:
            # Parse M3U8 video playlist
            m3u8_variant: m3u8.M3U8 = m3u8.load(manifest)
            # Find the desired video resolution or the next best one.
            m3u8_playlist, codecs = self._extract_video_stream(m3u8_variant, self.settings.data.quality_video.value)
            # Populate urls.
            stream_urls: list[str] = m3u8_playlist.files

            # TODO: Handle encryption key. But I have never seen an encrypted file so far.
            encryption_type: str = "NONE"
            encryption_key: str | None = None
        else:
            raise UnknownManifestFormat

        file_extension: str = self.get_file_extension(stream_urls[0], codecs)

        result: StreamManifest = StreamManifest(
            urls=stream_urls,
            codecs=codecs,
            file_extension=file_extension,
            encryption_type=encryption_type,
            encryption_key=encryption_key,
            mime_type=mime_type,
        )

        return result

    def _extract_video_stream(self, m3u8_variant: m3u8.M3U8, quality: str) -> (m3u8.M3U8 | bool, str):
        m3u8_playlist: m3u8.M3U8 | bool = False
        resolution_best: int = 0
        mime_type: str = ""

        if m3u8_variant.is_variant:
            for playlist in m3u8_variant.playlists:
                if resolution_best < playlist.stream_info.resolution[1]:
                    resolution_best = playlist.stream_info.resolution[1]
                    m3u8_playlist = m3u8.load(playlist.uri)
                    mime_type = playlist.stream_info.codecs

                    if quality == playlist.stream_info.resolution[1]:
                        break

        return m3u8_playlist, mime_type
