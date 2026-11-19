"""The Hi-Res track of a Bilibili video.

Two questions have to be kept apart: does the *video* carry a lossless
track, and may *this account* have it.  The server answers the first in
``dash.flac.display`` and the second in ``dash.flac.audio`` -- an account
without the membership gets the flag but no track, so a client that only
looks for the track cannot tell "this video has none" from "you may not
have it".
"""

from __future__ import annotations

from typing import Any

from src.core.bilibili import BiliClient


def make_client(answers: dict[str, dict[str, Any]]) -> tuple[BiliClient, list]:
    """A client whose HTTP layer answers with canned payloads."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_get(path: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        calls.append((path, dict(params or {})))
        for key, value in answers.items():
            if path == key:
                return value
        return {"code": -404, "message": f"no answer for {path}"}

    client = BiliClient()
    client._get = fake_get  # type: ignore[method-assign]
    return client, calls


#: The shape the server really sends: the ``id`` carries the track, the
#: ``codecid`` field is left at 0 (measured on a live answer), and the
#: ``codecs`` string names the codec.
def _flac_entry(base: str = "http://cdn/flac.m4s", bandwidth: int = 1_400_000) -> dict[str, Any]:
    return {
        "id": 30258,
        "codecid": 0,
        "baseUrl": base,
        "codecs": "flac",
        "mimeType": "audio/mp4",
        "bandwidth": bandwidth,
        "segment_base": {"initialization": "0-100"},
    }


def _dolby_entry() -> dict[str, Any]:
    return {
        "id": 30250,
        "codecid": 0,
        "baseUrl": "http://cdn/dolby.m4s",
        "codecs": "ec-3",
        "mimeType": "audio/mp4",
        "bandwidth": 384000,
    }


def _client(flac_block: dict[str, Any], audio: list[dict[str, Any]] | None = None,
            dolby_block: dict[str, Any] | None = None) -> BiliClient:
    answers = {
        "/x/web-interface/view": {
            "code": 0,
            "data": {
                "cid": 111,
                "duration": 60,
                "title": "视频",
                "pic": "http://i0.hdslb.com/cover.jpg",
                "owner": {"name": "UP", "mid": 42},
            },
        },
        "/x/player/wbi/playurl": {
            "code": 0,
            "data": {
                "quality": 80,
                "accept_quality": [16, 32, 64, 80],
                "dash": {
                    "video": [
                        {
                            "id": 80,
                            "baseUrl": "http://cdn/video-80.m4s",
                            "codecs": "avc1.640032",
                            "bandwidth": 2000000,
                            "width": 1920,
                            "height": 1080,
                        }
                    ],
                    "audio": audio if audio is not None else [
                        {
                            "id": 30280,
                            "baseUrl": "http://cdn/audio.m4s",
                            "codecs": "mp4a.40.2",
                            "bandwidth": 128000,
                        }
                    ],
                    "flac": flac_block,
                    "dolby": dolby_block if dolby_block is not None
                    else {"type": 0, "audio": None},
                },
            },
        },
    }
    client, _calls = make_client(answers)
    return client


# ----------------------------------------------------------------------
# the two questions


def test_a_video_with_a_lossless_track_says_so():
    """The flag is there even when the account may not have the track."""
    client = _client({"display": True, "audio": None})

    answer = client.video_streams("BV1xx411c7mD", quality=0, with_muxed=False)

    assert answer["hires_available"] is True
    assert answer["hires_granted"] is False
    assert answer["flac"] is None


def test_a_video_without_a_lossless_track_says_so():
    client = _client({"display": False, "audio": None})

    answer = client.video_streams("BV1xx411c7mD", quality=0, with_muxed=False)

    assert answer["hires_available"] is False
    assert answer["hires_granted"] is False
    assert answer["flac"] is None


def test_the_track_is_handed_over_when_the_account_may_have_it():
    client = _client({"display": True, "audio": [_flac_entry()]})

    answer = client.video_streams("BV1xx411c7mD", quality=0, with_muxed=False)

    assert answer["hires_available"] is True
    assert answer["hires_granted"] is True
    assert answer["flac"] is not None
    assert answer["flac"]["url"].endswith("flac.m4s")
    # The lossless track is FLAC; a client that cannot tell that will try to
    # hand it to a decoder that refuses it.
    assert "flac" in answer["flac"]["codecs"].lower()


# ----------------------------------------------------------------------
# the codec id is not the entry id


def test_an_ordinary_aac_track_is_not_mistaken_for_dolby():
    """The ids of the ordinary tracks are audio quality codes.

    30280 means "192 kbps AAC", and 30216 / 30232 mean 64 and 132 kbps.
    Reading one of them as a codec id made an ordinary track answer as the
    Dolby track, and the player then told the user the video had Dolby
    sound when it had none.
    """
    ordinary = [
        {
            "id": 30280,
            "baseUrl": "http://cdn/audio-192.m4s",
            "codecs": "mp4a.40.2",
            "bandwidth": 203786,
        },
        {
            "id": 30216,
            "baseUrl": "http://cdn/audio-64.m4s",
            "codecs": "mp4a.40.5",
            "bandwidth": 43962,
        },
    ]
    client = _client({"display": False, "audio": None}, audio=ordinary)

    answer = client.video_streams("BV1xx411c7mD", quality=0, with_muxed=False)

    assert answer["dolby"] is None
    assert answer["dolby_available"] is False
    # The ordinary track is still there -- only the Dolby claim was wrong.
    assert answer["audio"] is not None
    assert answer["audio"]["url"].endswith("audio-192.m4s")


def test_the_dolby_track_is_found_when_it_is_there():
    client = _client({"display": False, "audio": None},
                     dolby_block={"type": 1, "audio": [_dolby_entry()]})

    answer = client.video_streams("BV1xx411c7mD", quality=0, with_muxed=False)

    assert answer["dolby"] is not None
    assert answer["dolby"]["url"].endswith("dolby.m4s")
    assert answer["dolby_available"] is True


def test_the_lossless_track_is_found_in_the_ordinary_list_too():
    """Some servers put the FLAC track into ``dash.audio`` instead.

    There it is identified by its id (30258), which is *not* the codec id
    field -- that one is left at 0 by the server.  A client that looks at
    ``codecid`` finds nothing there and never reaches the track.
    """
    mixed = [
        {
            "id": 30280,
            "codecid": 0,
            "baseUrl": "http://cdn/audio-192.m4s",
            "codecs": "mp4a.40.2",
            "bandwidth": 203786,
        },
        {
            "id": 30258,
            "codecid": 0,
            "baseUrl": "http://cdn/flac.m4s",
            "codecs": "flac",
            "bandwidth": 1400000,
        },
    ]
    client = _client({"display": False, "audio": None}, audio=mixed)

    answer = client.video_streams("BV1xx411c7mD", quality=0, with_muxed=False)

    assert answer["flac"] is not None
    assert answer["flac"]["url"].endswith("flac.m4s")
    assert answer["hires_granted"] is True


def test_the_lossless_track_is_found_by_its_id_alone():
    """The id alone has to be enough.

    The ``codecs`` string is not always there -- and when it is not, a
    client that only reads it misses the track.  The id of the lossless
    track is 30258, which is neither one of the AAC bitrates nor the
    Dolby one.
    """
    mixed = [
        {
            "id": 30280,
            "baseUrl": "http://cdn/audio-192.m4s",
            "codecs": "mp4a.40.2",
            "bandwidth": 203786,
        },
        {
            "id": 30258,
            "baseUrl": "http://cdn/flac.m4s",
            "bandwidth": 1400000,
        },
    ]
    client = _client({"display": False, "audio": None}, audio=mixed)

    answer = client.video_streams("BV1xx411c7mD", quality=0, with_muxed=False)

    assert answer["flac"] is not None
    assert answer["flac"]["url"].endswith("flac.m4s")
    assert answer["hires_granted"] is True
