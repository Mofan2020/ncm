"""Core package: API client, signing and downloading."""

from src.core.api import NeteaseAPI, cap_quality, get_api, quality_fallbacks
from src.core.crypto import eapi_body, sign_eapi
from src.core.downloader import DownloadStats, DownloadTask, SongDownloader

__all__ = [
    "NeteaseAPI",
    "get_api",
    "cap_quality",
    "quality_fallbacks",
    "eapi_body",
    "sign_eapi",
    "SongDownloader",
    "DownloadTask",
    "DownloadStats",
]
