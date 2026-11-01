"""The Bilibili frontend fixes: the coin labels, the SMS captcha key, the backup.

None of these three defects shows up as a Python error.  A button without a
label is an empty box, a missing ``captcha_key`` is a rejected login, and a
backup that does not mention Bilibili is a backup that quietly loses half of
the account.  So they are pinned here.

The checks read the files, the way the other frontend tests do: the markup,
the script and the two catalogues are three links that have to agree.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "gui" / "assets"
I18N = ROOT / "src" / "i18n"

INDEX_HTML = (ASSETS / "index.html").read_text(encoding="utf-8")
APP_JS = (ASSETS / "app.js").read_text(encoding="utf-8")
PLAYER_HTML = (ASSETS / "bili-player.html").read_text(encoding="utf-8")
PLAYER_JS = (ASSETS / "bili-player-window.js").read_text(encoding="utf-8")


def _flat_catalogue(name: str) -> dict[str, str]:
    data = json.loads((I18N / f"{name}.json").read_text(encoding="utf-8"))
    flat: dict[str, str] = {}

    def walk(node: dict, prefix: str = "") -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                walk(value, path)
            else:
                flat[path] = value

    walk(data)
    return flat


#: The three coin buttons of the video detail panel and the label each of
#: them has to carry.
COIN_BUTTONS = (
    ("btn-bili-coin", "bili.coin"),
    ("btn-bili-coin-one", "bili.coin_one"),
    ("btn-bili-coin-two", "bili.coin_two"),
)

#: The same three buttons in the player window of its own.
PLAYER_COIN_BUTTONS = (
    ("bp-coin", "bili.coin"),
    ("bp-coin-one", "bili.coin_one"),
    ("bp-coin-two", "bili.coin_two"),
)

#: The other two buttons of the detail panel.  They carried no label at
#: all until the details of a video were loaded, so they stood empty.
DETAIL_BUTTONS = (
    ("btn-bili-like", "bili.like"),
    ("btn-bili-favorite", "bili.favorite_add"),
)


def _element(html: str, element_id: str) -> str:
    """The tag of the element with this id, as it stands in the markup."""
    match = re.search(r"<[^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*>", html)
    assert match, f"{element_id} is missing from the markup"
    return match.group(0)


def _markup_label(html: str, element_id: str) -> str:
    """The i18n key the markup gives this element, if it has one."""
    tag = _element(html, element_id)
    match = re.search(r'data-i18n="([^"]+)"', tag)
    return match.group(1) if match else ""


def _script_label(js: str, element_id: str) -> str:
    """The i18n key the script writes into this element, if it writes one."""
    match = re.search(
        r"getElementById\('" + re.escape(element_id) + r"'\)"
        r"[\s\S]{0,200}?textContent\s*=\s*t\('([^']+)'",
        js,
    )
    return match.group(1) if match else ""


def _call_arguments(js: str, call: str) -> list[str]:
    """The arguments of a bridge call, split at the top level commas."""
    start = js.find(call + "(")
    assert start >= 0, f"{call} is never called"
    index = start + len(call) + 1
    depth = 0
    args: list[str] = []
    current = ""
    while index < len(js):
        char = js[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                args.append(current.strip())
                return args
            depth -= 1
        if char == "," and depth == 0:
            args.append(current.strip())
            current = ""
            index += 1
            continue
        current += char
        index += 1
    raise AssertionError(f"the call to {call} is not closed")


# ----------------------------------------------------------- the SMS risk hint
def test_the_sms_login_offers_a_way_out_when_the_risk_control_fires():
    """风控要滑块的时候，界面得给条出路，不能只弹一个错误。

    The Bilibili risk control can demand the slider before it sends the
    code.  The interface has to say what happened and offer two ways out:
    the page to solve it on, or the QR code that needs no slider at all.
    """
    for element_id in ("bili-sms-risk", "btn-bili-sms-risk-open", "btn-bili-sms-risk-qr"):
        assert f'id="{element_id}"' in INDEX_HTML, f"{element_id} is missing"
    # The block starts hidden and is shown by the script.
    block = _element(INDEX_HTML, "bili-sms-risk")
    assert "hidden" in block
    assert "function setBiliSmsRisk" in APP_JS
    assert "captcha_required" in APP_JS
    # The QR code is the way out that needs no slider.
    assert "switchBiliLoginTab('qr')" in APP_JS


def test_the_sms_risk_buttons_are_wired():
    assert "'btn-bili-sms-risk-open'" in APP_JS
    assert "'btn-bili-sms-risk-qr'" in APP_JS
    assert "openBiliSmsRiskPage" in APP_JS
    assert "switchToBiliQrLogin" in APP_JS


# ------------------------------------------------------------ the catalogues
def test_every_translation_key_in_the_markup_exists_in_both_languages():
    """一个键缺了，界面上就是一个空白或者一串键名。

    The markup names its keys; every one of them has to be in both
    catalogues.  The two languages are checked separately -- a key that only
    exists in one of them shows up as the raw key in the other.
    """
    keys = set(re.findall(r'data-i18n(?:-title|-placeholder)?="([^"]+)"', INDEX_HTML))
    assert keys, "no translation keys found in the markup"
    for language in ("zh_cn", "en_us"):
        catalogue = _flat_catalogue(language)
        missing = sorted(key for key in keys if key not in catalogue)
        assert not missing, f"{language} is missing {missing}"
        empty = sorted(key for key in keys
                       if key in catalogue and not catalogue[key].strip())
        assert not empty, f"{language} has empty values for {empty}"


def test_both_catalogues_have_the_same_keys():
    """A key in one language and not in the other is a hole in the interface."""
    zh = set(_flat_catalogue("zh_cn"))
    en = set(_flat_catalogue("en_us"))
    assert not (zh - en), f"only in zh_cn: {sorted(zh - en)}"
    assert not (en - zh), f"only in en_us: {sorted(en - zh)}"


# --------------------------------------------------------- the other two buttons
def test_the_like_and_the_favorite_button_carry_a_label():
    """点赞和收藏两个按钮以前是空的，直到视频详情加载完才有字。

    A button without a word is a button nobody dares to press, and the
    detail panel is empty until a video is open.
    """
    for element_id, _key in DETAIL_BUTTONS:
        found = set()
        markup = _markup_label(INDEX_HTML, element_id)
        if markup:
            found.add(markup)
        script = _script_label(APP_JS, element_id)
        if script:
            found.add(script)
        assert found, f"{element_id} carries no label at all"
        for language in ("zh_cn", "en_us"):
            catalogue = _flat_catalogue(language)
            for name in found:
                assert name in catalogue, f"{name} is missing from {language}"
                assert catalogue[name].strip(), f"{name} is empty in {language}"


def test_the_detail_buttons_get_a_label_before_a_video_is_open():
    """The fallback that fills the buttons before the details arrive."""
    assert "function renderBiliDetailButtons" in APP_JS
    # It has to be called when the Bilibili area is drawn.
    call = re.search(
        r"async function renderBili\([^)]*\)\s*\{(.*?)\n\}", APP_JS, re.S)
    assert call, "renderBili is missing"
    assert "renderBiliDetailButtons()" in call.group(1)


# --------------------------------------------------------------- the coin labels
def test_the_three_coin_buttons_exist():
    """投币按钮必须都在。"""
    for element_id, _ in COIN_BUTTONS:
        assert f'id="{element_id}"' in INDEX_HTML, f"{element_id} is missing"


def test_every_coin_button_carries_a_label():
    """A button without a label is an empty box nobody dares to press.

    The label may come from the markup (``data-i18n``) or from the script
    (``textContent = t(...)``); both paths are accepted, but at least one of
    them has to name a key that exists -- and is not empty -- in **both**
    languages.  The keys are collected from the files, never assumed.
    """
    for element_id, _ in COIN_BUTTONS:
        found = set()
        markup = _markup_label(INDEX_HTML, element_id)
        if markup:
            found.add(markup)
        script = _script_label(APP_JS, element_id)
        if script:
            found.add(script)
        assert found, f"{element_id} carries no label at all"
        for language in ("zh_cn", "en_us"):
            catalogue = _flat_catalogue(language)
            for used in found:
                assert used in catalogue, f"{language} misses {used}"
                assert catalogue[used].strip(), f"{language}:{used} is empty"


def test_the_coin_buttons_are_labelled_in_the_script():
    """The script names them like it names the like and the favourite button."""
    for element_id, key in COIN_BUTTONS:
        assert _script_label(APP_JS, element_id) == key, (
            f"{element_id} is not labelled with {key} in app.js")


def test_the_player_window_coin_buttons_carry_their_labels():
    """独立播放窗口里的三个投币按钮也要有文字。"""
    for element_id, key in PLAYER_COIN_BUTTONS:
        assert f'id="{element_id}"' in PLAYER_HTML, f"{element_id} is missing"
        assert f"'{element_id}'" in PLAYER_JS, f"{element_id} is never written"
        assert f"'{key}'" in PLAYER_JS, f"{key} is never used in the player window"
        for language in ("zh_cn", "en_us"):
            catalogue = _flat_catalogue(language)
            assert catalogue.get(key, "").strip(), f"{language} misses {key}"


def _extract_function(js: str, name: str) -> str:
    """The source of one function, braces counted."""
    start = js.find(f"function {name}(")
    assert start >= 0, f"{name} is not defined"
    index = js.find("{", start)
    depth = 0
    while index < len(js):
        if js[index] == "{":
            depth += 1
        elif js[index] == "}":
            depth -= 1
            if depth == 0:
                return js[start:index + 1]
        index += 1
    raise AssertionError(f"the function {name} is not closed")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_player_window_never_writes_a_raw_key():
    """翻译没到之前不能把 "bili.coin" 写到按钮上。

    The window paints its labels before the catalogue arrives.  A label that
    falls back to the key itself shows the user ``bili.coin`` for a moment --
    so an unknown key has to leave the button empty.  The check runs the
    window's own ``applyStaticText()`` under node: once without a catalogue
    and once with one.
    """
    source = "\n".join([
        _extract_function(PLAYER_JS, "label"),
        _extract_function(PLAYER_JS, "setLabel"),
        _extract_function(PLAYER_JS, "setPlaceholder"),
        _extract_function(PLAYER_JS, "applyStaticText"),
    ])
    harness = """
const state = {translations: {}};
const nodes = {};
function el(id) {
    if (!nodes[id]) {
        nodes[id] = {textContent: null, attributes: {},
            setAttribute: function (key, value) { this.attributes[key] = value; }};
    }
    return nodes[id];
}
const document = {title: ''};
function t(key) { return state.translations[key] || key; }
""" + source + """
applyStaticText();
const withoutCatalogue = nodes['bp-coin'] ? nodes['bp-coin'].textContent : null;
state.translations['bili.coin'] = '投币';
applyStaticText();
const withCatalogue = nodes['bp-coin'] ? nodes['bp-coin'].textContent : null;
console.log(JSON.stringify({withoutCatalogue, withCatalogue}));
"""
    result = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["withoutCatalogue"] in (None, ""), (
        "the player window writes the raw key into the button")
    assert payload["withCatalogue"] == "投币", (
        "the player window does not write the translation into the button")


def test_the_player_window_does_not_label_with_the_raw_key():
    """The static labels go through the helper, not through the raw key."""
    for element_id, _key in PLAYER_COIN_BUTTONS:
        assert not re.search(
            r"el\('" + re.escape(element_id) + r"'\)\.textContent\s*=\s*t\('",
            PLAYER_JS), f"{element_id} still writes the raw key"


# --------------------------------------------------------------- the SMS captcha key
def test_the_sms_login_sends_the_captcha_key():
    """短信登录要把发码时拿到的 captcha_key 一起送回去。"""
    args = _call_arguments(APP_JS, "state.api.bili_login_sms")
    assert len(args) == 4, (
        f"bili_login_sms is called with {len(args)} arguments, 4 are needed")
    assert args[2] == "'86'", "the country code is the fourth argument's job"
    assert "captcha" in args[3].lower(), (
        f"the fourth argument is {args[3]!r}, not the captcha key")


def test_the_captcha_key_comes_from_the_send_step():
    """发码成功之后要把 captcha_key 存起来。"""
    assert re.search(r"bili_send_sms_code\(", APP_JS)
    assert re.search(r"captcha_key", APP_JS), "the captcha key is never read"
    assert re.search(
        r"biliLogin\.smsCaptchaKey\s*=\s*result\.captcha_key", APP_JS), (
        "the captcha key of the send step is not kept")


def test_a_failed_send_drops_the_old_captcha_key():
    """发码失败之后，旧 key 不能留在手上。"""
    assert re.search(
        r"biliLogin\.smsCaptchaKey\s*=\s*''", APP_JS), (
        "the captcha key is never invalidated")


def test_another_phone_number_drops_the_captcha_key():
    """换了手机号，旧 key 就跟这个号码没关系了。"""
    assert re.search(
        r"biliLogin\.smsTel\s*===\s*tel", APP_JS), (
        "the captcha key is not bound to the phone number")
    assert re.search(r"bili-phone", APP_JS), (
        "the phone number is never watched")


# --------------------------------------------------------------- the backup
def test_the_backup_preview_has_a_bilibili_section():
    """导入确认里要有一块专门说 B 站。"""
    for element_id in ("backup-bili-block", "backup-bili-summary",
                       "backup-bili-list"):
        assert f'id="{element_id}"' in INDEX_HTML, f"{element_id} is missing"
    assert "renderBackupBilibili" in APP_JS, "the Bilibili plan is never drawn"
    assert "plan.bilibili" in APP_JS, "the Bilibili plan is never read"


def test_the_export_counts_the_bilibili_half():
    """导出之后要报出 B 站那一半有多大。"""
    assert "result.bilibili_folders" in APP_JS
    assert "result.bilibili_videos" in APP_JS
    for key in ("backup.export_bili", "backup.export_bili_only"):
        for language in ("zh_cn", "en_us"):
            catalogue = _flat_catalogue(language)
            assert catalogue.get(key, "").strip(), f"{language} misses {key}"


def test_the_backup_words_say_whether_bilibili_is_restored():
    """只登录网易云或只登录 B 站时，要把话说清楚。"""
    for key in ("backup.bili_restore", "backup.bili_skip",
                "backup.netease_skip", "backup.bili_summary"):
        for language in ("zh_cn", "en_us"):
            catalogue = _flat_catalogue(language)
            assert catalogue.get(key, "").strip(), f"{language} misses {key}"
    assert "backup.bili_restore" in APP_JS
    assert "backup.bili_skip" in APP_JS
    assert "backup.netease_skip" in APP_JS
