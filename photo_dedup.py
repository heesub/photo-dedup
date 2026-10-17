#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
photo_dedup.py - find near-duplicate photos, keep the best copy and quarantine the rest.

How it works
  1) Scan (index): for every photo, extract resolution, estimated JPEG quality, EXIF (capture time,
     camera, GPS), a perceptual hash (pHash) and a 64x64 grayscale thumbnail into an SQLite cache.
     - Files whose (path, size, mtime) are unchanged are not read again.
     - Files moved to another folder are recognized by (name, size, mtime) and reused.
  2) Find duplicates: pick candidates by pHash Hamming distance, then verify by comparing the
     thumbnails directly (mean absolute difference and correlation).
     - Burst protection: if both photos carry camera EXIF and their capture times
       (seconds + sub-seconds) differ, they are never treated as duplicates.
     - Photos with a different aspect ratio (crops) are never treated as duplicates.
  3) Choose the copy to keep: resolution -> metadata (camera EXIF, capture time, GPS)
     -> JPEG quality -> file size.
  4) Quarantine: the other copies are moved to <root>/_quarantine/<original relative path>.
     Every move is recorded and can be restored.

Groups with a certain verdict are marked 'auto'; groups a human should check are marked 'review'.
Only 'auto' groups start out approved; everything can be reviewed and changed in the GUI.

Usage
  python3 photo_dedup.py                       # start the GUI
  python3 photo_dedup.py gui /mnt/d/Photos     # start the GUI on a folder
  python3 photo_dedup.py scan /mnt/d/Photos -j 6
  python3 photo_dedup.py find /mnt/d/Photos --csv groups.csv
  python3 photo_dedup.py quarantine /mnt/d/Photos [--include-review] [--copy-meta]
  python3 photo_dedup.py restore /mnt/d/Photos
  python3 photo_dedup.py --lang ko gui         # Korean UI (or PHOTODEDUP_LANG=ko)

Requirements: numpy, pillow  (HEIC: pillow-heif, GUI: python3-tk, metadata copy: exiftool)
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import multiprocessing as mp
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import zlib
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    HEIF = True
except ImportError:
    HEIF = False

Image.MAX_IMAGE_PIXELS = None  # allow very large panoramas

EXTS = {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff", ".bmp"}
if HEIF:
    EXTS |= {".heic", ".heif"}
LOSSLESS = {"PNG", "TIFF", "BMP"}
Q_DIR = "_quarantine"
SKIP_DIRS = {Q_DIR, "_dups", "$RECYCLE.BIN", "System Volume Information", "@eaDir"}
DB_NAME = ".photodedup.sqlite"
SIDECAR_EXTS = {".aae", ".xmp"}
THUMB = 64

# Sensitivity presets: pHash candidate distance, max mean abs difference, min correlation,
# and the difference below which a match counts as 'certain'.
#   - recompressed / resized / metadata-stripped copies: diff 0.2-1.1, corr >= 0.9996 (test set)
#   - handheld burst frames: diff >= 2.1, corr <= 0.9988
PRESETS = {
    "strict": dict(cand=8, mae=1.5, corr=0.9990, cert=0.8),
    "normal": dict(cand=10, mae=2.5, corr=0.9970, cert=1.2),
    "loose": dict(cand=12, mae=4.0, corr=0.9900, cert=1.6),
}
PRESET_ALIASES = {"엄격": "strict", "보통": "normal", "느슨": "loose"}
DEFAULT_PRESET = "normal"

# ----------------------------------------------------------------------------
# Localization
# ----------------------------------------------------------------------------

LANGS = {"en": "English", "ko": "한국어"}

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # engine / progress
        "walk_done": "Folder scan complete",
        "to_index": "{n} files to analyze",
        "find_prep": "Preparing to compare {n} photos",
        "find_cand": "Finding candidates {i}/{n}",
        "find_verify": "Verifying {i}/{n}",
        "find_done": "{n} duplicate groups",
        "err_keeper_missing": "Skipped group {gid}: kept file is missing: {path}",
        "err_meta": "Could not copy capture info into {path}: {e}",
        "err_missing": "File not found: {path}",
        "err_move": "Could not move {path}: {e}",
        "err_q_missing": "Quarantined file is gone (already deleted?): {path}",
        "err_dest_exists": "A file already exists at the original location: {path}",
        "no_exiftool": "exiftool not found",
        "open_failed": "Could not open: {e}",
        # GUI
        "title": "Photo Near-Duplicate Cleaner",
        "folder": "Photo folder",
        "browse": "Browse…",
        "open": "Open",
        "sensitivity": "Sensitivity",
        "preset_strict": "Strict",
        "preset_normal": "Normal",
        "preset_loose": "Loose",
        "burst": "Burst protection (different camera capture times = not duplicates)",
        "copymeta": "Copy missing capture info from quarantined copies into the kept file (exiftool)",
        "workers": "Worker processes",
        "language": "Language",
        "btn_scan": "① Scan (analyze new photos)",
        "btn_find": "② Find duplicates",
        "btn_quar": "③ Quarantine approved groups",
        "btn_restore": "Restore all quarantined",
        "btn_csv": "Export CSV",
        "btn_stop": "Stop",
        "view": "Show",
        "filter_all": "All",
        "filter_auto": "Auto",
        "filter_review": "Review",
        "filter_approved": "Approved",
        "filter_unapproved": "Not approved",
        "filter_done": "Done",
        "col_n": "Files",
        "col_status": "Verdict",
        "col_ok": "Approved",
        "col_save": "To free",
        "col_keep": "Kept file",
        "status_auto": "Auto",
        "status_review": "Review",
        "btn_toggle": "Approve / unapprove selected (Space)",
        "btn_all_auto": "Approve all auto groups",
        "btn_all": "Approve all groups",
        "btn_none": "Unapprove all",
        "head_empty": "Select a group to see its photos side by side here.",
        "hint": "Double-click a photo to open it in the default app. Use Keep / Quarantine to change "
                "a decision, and Exclude for photos that are not duplicates.",
        "status_start": "Choose a photo folder and press ① Scan.",
        "no_folder_title": "Folder not found",
        "no_folder": "Not a folder:\n{p}\n\nIn WSL, enter /mnt/d/Photos instead of D:\\Photos.",
        "db_label": "Cache: {name} · analyzed {ok} · errors {err} · quarantined {q}",
        "log_open": "Opened folder: {p}  ({ok} photos cached)",
        "phase_walk": "Scanning folders",
        "phase_index": "Analyzing",
        "phase_find": "Comparing",
        "phase_quarantine": "Quarantining",
        "phase_restore": "Restoring",
        "walk_progress": "{phase}: {n} files found  {text}",
        "error_title": "Error",
        "scan_stopped": "Scan stopped (results so far have been saved)",
        "scan_done": "Scan complete: {total} photos / newly analyzed {indexed} / reused from cache "
                     "{reused} (moved {moved}) / removed missing {removed} / read errors {errors}",
        "walk_err": "  Could not read folder: {e}",
        "status_scan_done": "Scan finished. Press ② Find duplicates.",
        "find_log": "{n} duplicate groups ({auto} auto, {review} review)",
        "status_find_done": "Review the groups, then press ③ Quarantine. Yellow = review recommended",
        "quar_log": "Quarantined {n} files, {size}, capture info copied for {meta} -> {dir}",
        "status_quar_done": "Quarantined {n} files. Check them, then delete the _quarantine folder yourself.",
        "restore_log": "Restored {n} files",
        "status_restore_done": "Restored. Run ① Scan again.",
        "log_scan_start": "Scanning…",
        "log_find_start": "Finding duplicates (sensitivity: {p}, burst protection: {b})…",
        "on": "on",
        "off": "off",
        "quar_title": "Quarantine",
        "quar_none": "There are no approved groups.",
        "quar_confirm": "Move {n} files ({size}) from {g} approved groups to\n{dir}.",
        "quar_confirm_review": "\n\n{n} of these groups are 'Review' groups.",
        "quar_confirm_meta": "\n\nIf a kept file has no capture info, it is copied from a quarantined copy "
                             "(the original is backed up in _meta_backup).",
        "restore_title": "Restore",
        "restore_none": "There is nothing to restore.",
        "restore_confirm": "Move {n} quarantined files back to their original locations.",
        "csv_need_find": "Run ② Find duplicates first.",
        "csv_saved": "CSV saved: {p}",
        "none": "(none)",
        "summary": "Groups {g} · approved {a} · to quarantine {n} files ({size})",
        "head": "Group {gid} · {n} photos · verdict: {st} · {ap} · max difference {mae}",
        "head_auto": "Auto (certain)",
        "head_review": "Review recommended",
        "approved": "Approved ✔",
        "not_approved": "Not approved",
        "head_done": " · Done",
        "no_preview": "(no preview)",
        "card_keep": "Keep",
        "card_q": "Quarantine",
        "card_best": "  (recommended)",
        "card_size": "{w}×{h}  {size}  {fmt}  quality≈{q}",
        "card_taken": "Taken: {t}",
        "card_camera": "Camera: {c}",
        "card_noexif": "  (no capture info)",
        "card_gps": "GPS: {v}",
        "yes": "yes",
        "no": "no",
        "card_meta_note": "※ Capture info will be copied into this file from a quarantined copy",
        "btn_to_q": "Quarantine",
        "btn_to_keep": "Keep",
        "btn_exclude": "Exclude",
        "btn_folder": "Folder",
        "excluded_log": "Group {gid}: recorded as not duplicates.",
        "no_kfont": "No Korean font found. In WSL: sudo apt install fonts-noto-cjk  (then restart)",
        # CLI
        "cli_desc": "Find near-duplicate photos and quarantine all but the best copy.",
        "help_lang": "UI language: en or ko (default: en, or the PHOTODEDUP_LANG environment variable)",
        "help_root": "photo folder",
        "help_workers": "number of worker processes",
        "help_retry": "re-analyze files that failed to read before",
        "help_preset": "sensitivity preset",
        "help_noburst": "turn off burst protection",
        "help_csv": "write the group list to this CSV file",
        "help_review": "also quarantine 'review' groups",
        "help_meta": "copy missing capture info into kept files (exiftool)",
        "not_dir": "Not a folder: {p}  (in WSL use /mnt/d/... paths)",
        "cli_find": "{n} groups ({auto} auto, {review} review), {q} files to quarantine ({size})",
        "cli_quar": "Quarantined {n} files ({size}), capture info copied for {meta}",
        "cli_restore": "Restored {n} files",
    },
    "ko": {
        "walk_done": "폴더 탐색 완료",
        "to_index": "새로 분석할 파일 {n}개",
        "find_prep": "사진 {n}장 비교 준비",
        "find_cand": "후보 탐색 {i}/{n}",
        "find_verify": "정밀 비교 {i}/{n}",
        "find_done": "중복 그룹 {n}개",
        "err_keeper_missing": "보존 파일이 없어 그룹 {gid} 건너뜀: {path}",
        "err_meta": "촬영정보 복사 실패 {path}: {e}",
        "err_missing": "파일 없음: {path}",
        "err_move": "이동 실패 {path}: {e}",
        "err_q_missing": "격리 파일 없음(이미 삭제?): {path}",
        "err_dest_exists": "원래 위치에 이미 파일 있음: {path}",
        "no_exiftool": "exiftool 없음",
        "open_failed": "열기 실패: {e}",
        "title": "사진 유사 중복 정리",
        "folder": "사진 폴더",
        "browse": "찾아보기…",
        "open": "열기",
        "sensitivity": "민감도",
        "preset_strict": "엄격",
        "preset_normal": "보통",
        "preset_loose": "느슨",
        "burst": "연사 보호 (카메라 촬영시각이 다르면 중복 아님)",
        "copymeta": "보존 파일에 없는 촬영정보를 격리 파일에서 복사 (exiftool)",
        "workers": "작업 프로세스",
        "language": "언어",
        "btn_scan": "① 스캔 (새 사진 분석)",
        "btn_find": "② 중복 찾기",
        "btn_quar": "③ 승인된 그룹 격리 실행",
        "btn_restore": "격리 전부 복원",
        "btn_csv": "CSV 내보내기",
        "btn_stop": "중지",
        "view": "보기",
        "filter_all": "전체",
        "filter_auto": "자동",
        "filter_review": "검토",
        "filter_approved": "승인됨",
        "filter_unapproved": "미승인",
        "filter_done": "처리 완료",
        "col_n": "장수",
        "col_status": "판정",
        "col_ok": "승인",
        "col_save": "격리 용량",
        "col_keep": "보존 파일",
        "status_auto": "자동",
        "status_review": "검토",
        "btn_toggle": "선택 그룹 승인/해제 (Space)",
        "btn_all_auto": "자동 그룹 모두 승인",
        "btn_all": "모든 그룹 승인",
        "btn_none": "모두 승인 해제",
        "head_empty": "그룹을 선택하면 사진이 여기에 나란히 보입니다.",
        "hint": "사진을 더블클릭하면 기본 앱으로 엽니다. ‘보존으로/격리로’로 결정을 바꾸고, "
                "중복이 아닌 사진은 ‘그룹 제외’ 하세요.",
        "status_start": "사진 폴더를 선택하고 ① 스캔을 누르세요.",
        "no_folder_title": "폴더 없음",
        "no_folder": "폴더가 아닙니다:\n{p}\n\nWSL에서는 D:\\Photos 대신 /mnt/d/Photos 처럼 입력하세요.",
        "db_label": "캐시: {name} · 분석됨 {ok}장 · 오류 {err} · 격리 {q}",
        "log_open": "폴더 열기: {p}  (캐시 {ok}장)",
        "phase_walk": "폴더 탐색",
        "phase_index": "분석",
        "phase_find": "비교",
        "phase_quarantine": "격리",
        "phase_restore": "복원",
        "walk_progress": "{phase}: 파일 {n}개 발견  {text}",
        "error_title": "오류",
        "scan_stopped": "스캔 중지됨 (지금까지 분석한 결과는 저장되었습니다)",
        "scan_done": "스캔 완료: 사진 {total}장 / 새로 분석 {indexed} / 캐시 재사용 {reused} "
                     "(이동 감지 {moved}) / 사라진 파일 정리 {removed} / 읽기 오류 {errors}",
        "walk_err": "  폴더 읽기 실패: {e}",
        "status_scan_done": "스캔이 끝났습니다. ② 중복 찾기를 누르세요.",
        "find_log": "중복 그룹 {n}개 (자동 {auto}, 검토 {review})",
        "status_find_done": "그룹을 검토한 뒤 ③ 격리 실행을 누르세요. 노란색 = 검토 권장",
        "quar_log": "격리 완료: {n}개, {size}, 촬영정보 보완 {meta}개 -> {dir}",
        "status_quar_done": "{n}개를 격리했습니다. 확인 후 _quarantine 폴더를 직접 삭제하세요.",
        "restore_log": "복원 완료: {n}개",
        "status_restore_done": "복원했습니다. 다시 ① 스캔을 실행하세요.",
        "log_scan_start": "스캔 시작…",
        "log_find_start": "중복 찾기 (민감도 {p}, 연사 보호 {b})…",
        "on": "켬",
        "off": "끔",
        "quar_title": "격리 실행",
        "quar_none": "승인된 그룹이 없습니다.",
        "quar_confirm": "승인된 그룹 {g}개에서 {n}개 파일({size})을\n{dir} 로 옮깁니다.",
        "quar_confirm_review": "\n\n그중 '검토' 그룹이 {n}개 포함되어 있습니다.",
        "quar_confirm_meta": "\n\n보존 파일에 촬영정보가 없으면 격리 파일에서 복사합니다 (원본은 _meta_backup에 백업).",
        "restore_title": "복원",
        "restore_none": "복원할 격리 파일이 없습니다.",
        "restore_confirm": "격리된 {n}개 파일을 원래 위치로 되돌립니다.",
        "csv_need_find": "먼저 ② 중복 찾기를 실행하세요.",
        "csv_saved": "CSV 저장: {p}",
        "none": "(없음)",
        "summary": "그룹 {g} · 승인 {a} · 격리 예정 {n}개 ({size})",
        "head": "그룹 {gid} · {n}장 · 판정: {st} · {ap} · 최대 차이 {mae}",
        "head_auto": "자동 (확실)",
        "head_review": "검토 권장",
        "approved": "승인됨 ✔",
        "not_approved": "미승인",
        "head_done": " · 처리 완료",
        "no_preview": "(미리보기 불가)",
        "card_keep": "보존",
        "card_q": "격리",
        "card_best": "  (자동 추천)",
        "card_size": "{w}×{h}  {size}  {fmt}  화질≈{q}",
        "card_taken": "촬영: {t}",
        "card_camera": "카메라: {c}",
        "card_noexif": "  (촬영정보 없음)",
        "card_gps": "GPS: {v}",
        "yes": "있음",
        "no": "없음",
        "card_meta_note": "※ 격리 파일의 촬영정보를 이 파일에 복사합니다",
        "btn_to_q": "격리로",
        "btn_to_keep": "보존으로",
        "btn_exclude": "그룹 제외",
        "btn_folder": "폴더",
        "excluded_log": "그룹 {gid}: 중복이 아닌 것으로 기록했습니다.",
        "no_kfont": "한글 글꼴을 찾지 못했습니다. WSL에서: sudo apt install fonts-noto-cjk  (설치 후 다시 실행)",
        "cli_desc": "사실상 같은 사진을 찾아 가장 좋은 한 장만 남기고 나머지를 격리합니다.",
        "help_lang": "화면 언어: en 또는 ko (기본: en, 또는 환경변수 PHOTODEDUP_LANG)",
        "help_root": "사진 폴더",
        "help_workers": "작업 프로세스 수",
        "help_retry": "읽기 오류였던 파일도 다시 분석",
        "help_preset": "민감도 프리셋",
        "help_noburst": "연사 보호 끄기",
        "help_csv": "그룹 목록 CSV 저장 경로",
        "help_review": "'검토' 그룹도 격리",
        "help_meta": "보존 파일에 촬영정보 보완 (exiftool)",
        "not_dir": "폴더가 아닙니다: {p}  (WSL에서는 /mnt/d/... 형식으로)",
        "cli_find": "그룹 {n}개 (자동 {auto}, 검토 {review}), 격리 대상 {q}개 ({size})",
        "cli_quar": "격리 {n}개 ({size}), 촬영정보 보완 {meta}개",
        "cli_restore": "복원 {n}개",
    },
}

_LANG = "en"
SETTINGS_PATH = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "photodedup" / "settings.json"


def set_lang(code: str | None):
    global _LANG
    if code in MESSAGES:
        _LANG = code


def T(key: str, **kw) -> str:
    """Return the message for the current language (falls back to English)."""
    s = MESSAGES[_LANG].get(key) or MESSAGES["en"].get(key, key)
    return s.format(**kw) if kw else s


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_settings(**kw):
    try:
        s = _load_settings()
        s.update(kw)
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _initial_lang(argv: list[str]) -> str:
    """Language priority: --lang option > PHOTODEDUP_LANG > saved GUI choice > English."""
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--lang="):
            return a.split("=", 1)[1]
    env = os.environ.get("PHOTODEDUP_LANG", "").strip().lower()
    if env:
        return env[:2]
    return _load_settings().get("lang", "en")


# ----------------------------------------------------------------------------
# Image analysis (runs in worker processes)
# ----------------------------------------------------------------------------


def _dct_mat(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0] /= np.sqrt(2.0)
    return m


_D32 = _dct_mat(32)
_STD_LUM = np.array(
    [16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
     14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
     18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
     49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99],
    dtype=np.float64,
)


def _phash(g32: np.ndarray) -> bytes:
    d = _D32 @ g32 @ _D32.T
    low = d[:8, :8].flatten()
    med = np.median(low[1:])
    bits = (low > med).astype(np.uint8)
    return np.packbits(bits).tobytes()  # 8 bytes, big-endian bit order


def _jpeg_quality(im) -> float | None:
    """Estimate the libjpeg-style quality (1-100) from the JPEG quantization table."""
    q = getattr(im, "quantization", None)
    if not q:
        return None
    t = q.get(0) if 0 in q else next(iter(q.values()))
    t = np.asarray(t, dtype=np.float64)
    if t.size != 64:
        return None
    s = t.sum() / _STD_LUM.sum() * 100.0
    qual = (200.0 - s) / 2.0 if s <= 100 else 5000.0 / s
    return float(min(100.0, max(1.0, qual)))


def _s(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "ignore")
    v = str(v).replace("\x00", "").strip()
    return v or None


def _decode_small(im, size: int):
    """Downscaled, orientation-corrected image. JPEGs use fast DCT-domain downscaling."""
    W, H = im.size
    if im.format == "JPEG":
        im.draft(im.mode if im.mode in ("L", "RGB") else "RGB", (size, size))
        w, h = im.size
        if (w, h) != (W, H):
            # Sizes that are not multiples of 8 decode with padded edge blocks; crop to the real area
            sc = max(1, round(W / w))
            cw, ch = max(1, int(W / sc)), max(1, int(H / sc))
            if (cw, ch) != (w, h):
                im.load()
                exif = im.getexif()
                im = im.crop((0, 0, cw, ch))
                o = exif.get(0x0112, 1) if exif else 1
                method = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
                          4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
                          6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
                          8: Image.Transpose.ROTATE_90}.get(o)
                return im.transpose(method) if method else im
    return ImageOps.exif_transpose(im)


def index_file(path: str) -> dict:
    """Analyze one photo and return the values to store in the cache."""
    try:
        with Image.open(path) as im:
            fmt = im.format or ""
            w, h = im.size
            exif = im.getexif()
            orient = exif.get(0x0112, 1) if exif else 1
            if orient in (5, 6, 7, 8):
                w, h = h, w
            try:
                ifd = exif.get_ifd(0x8769) if exif else {}
            except Exception:
                ifd = {}
            make = _s(exif.get(0x010F)) if exif else None
            model = _s(exif.get(0x0110)) if exif else None
            software = _s(exif.get(0x0131)) if exif else None
            dto = _s(ifd.get(0x9003))
            subsec = _s(ifd.get(0x9291))
            if dto and (not dto[:4].isdigit() or dto.startswith("0000")):
                dto = None
            genuine = bool(make or model) and any(t in ifd for t in (0x829A, 0x829D, 0x8827))
            has_gps = bool(exif) and 0x8825 in exif
            if fmt == "JPEG":
                quality = _jpeg_quality(im)
            elif fmt in LOSSLESS:
                quality = 100.0
            else:
                quality = None

            small = _decode_small(im, 256)
            if small.mode not in ("L", "RGB"):
                small = small.convert("RGB")
            small = small.convert("L")
            small.thumbnail((256, 256), Image.Resampling.BILINEAR)
            g32 = np.asarray(small.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
            ph = _phash(g32)
            th = small.resize((THUMB, THUMB), Image.Resampling.LANCZOS)
            thumb = zlib.compress(th.tobytes(), 6)  # 64x64 grayscale, lossless
        return dict(
            status="ok", err=None, fmt=fmt, width=w, height=h, quality=quality,
            make=make, model=model, software=software, dto=dto, subsec=subsec,
            genuine=int(genuine), has_gps=int(has_gps), phash=ph, thumb=thumb,
        )
    except Exception as e:  # corrupt files etc.
        return dict(status="error", err=f"{type(e).__name__}: {e}"[:300])


# ----------------------------------------------------------------------------
# Cache store
# ----------------------------------------------------------------------------

FILE_COLS = ["fmt", "width", "height", "quality", "make", "model", "software", "dto", "subsec",
             "genuine", "has_gps", "phash", "thumb", "status", "err"]


class Store:
    def __init__(self, root: Path, db_path: Path | None = None):
        self.root = Path(root).resolve()
        self.path = Path(db_path) if db_path else self.root / DB_NAME
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                fmt TEXT, width INTEGER, height INTEGER, quality REAL,
                make TEXT, model TEXT, software TEXT, dto TEXT, subsec TEXT,
                genuine INTEGER, has_gps INTEGER, phash BLOB, thumb BLOB,
                status TEXT, err TEXT);
            CREATE INDEX IF NOT EXISTS idx_files_nsm ON files(name, size, mtime_ns);
            CREATE TABLE IF NOT EXISTS not_dup (a INTEGER, b INTEGER, PRIMARY KEY(a, b));
            CREATE TABLE IF NOT EXISTS prefs (id INTEGER PRIMARY KEY, choice TEXT);
            CREATE TABLE IF NOT EXISTS quarantine (
                qid INTEGER PRIMARY KEY, kind TEXT, orig TEXT, qpath TEXT, keeper TEXT,
                ts TEXT, restored INTEGER DEFAULT 0, sidecars TEXT);
            """
        )
        self.db.commit()

    def rel(self, p: Path | str) -> str:
        return Path(p).resolve().relative_to(self.root).as_posix()

    def abs(self, rel: str) -> Path:
        return self.root / rel

    def close(self):
        with self.lock:
            self.db.close()

    # --- user decisions -----------------------------------------------------
    def add_not_dup(self, a: int, b: int):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO not_dup VALUES (?,?)", (min(a, b), max(a, b)))
            self.db.commit()

    def not_dup_pairs(self) -> set[tuple[int, int]]:
        with self.lock:
            return {(a, b) for a, b in self.db.execute("SELECT a, b FROM not_dup")}

    def set_pref(self, fid: int, choice: str | None):
        with self.lock:
            if choice is None:
                self.db.execute("DELETE FROM prefs WHERE id=?", (fid,))
            else:
                self.db.execute("INSERT OR REPLACE INTO prefs VALUES (?,?)", (fid, choice))
            self.db.commit()

    def prefs(self) -> dict[int, str]:
        with self.lock:
            return dict(self.db.execute("SELECT id, choice FROM prefs"))

    def stats(self) -> dict:
        with self.lock:
            n_ok = self.db.execute("SELECT COUNT(*) FROM files WHERE status='ok'").fetchone()[0]
            n_err = self.db.execute("SELECT COUNT(*) FROM files WHERE status='error'").fetchone()[0]
            n_q = self.db.execute(
                "SELECT COUNT(*) FROM quarantine WHERE kind='dup' AND restored=0").fetchone()[0]
        return dict(ok=n_ok, error=n_err, quarantined=n_q)


# ----------------------------------------------------------------------------
# 1) Scan
# ----------------------------------------------------------------------------

def _noop(*a, **k):
    pass


def scan(store: Store, workers: int = 4, progress=_noop, stop: threading.Event | None = None,
         retry_errors: bool = False) -> dict:
    root = store.root
    stop = stop or threading.Event()

    # 1. Walk the folder tree
    found: dict[str, tuple[int, int, str]] = {}
    errors = []
    t0 = time.time()

    def onerr(e):
        errors.append(f"{e.filename}: {e.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerr):
        if stop.is_set():
            return dict(stopped=True)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in EXTS:
                continue
            ap = os.path.join(dirpath, fn)
            try:
                st = os.stat(ap)
            except OSError as e:
                errors.append(f"{ap}: {e.strerror}")
                continue
            rel = Path(ap).relative_to(root).as_posix()
            found[rel] = (st.st_size, st.st_mtime_ns, fn)
        if time.time() - t0 > 0.5:
            progress("walk", len(found), 0, dirpath)
            t0 = time.time()
    progress("walk", len(found), 0, T("walk_done"))

    # 2. Match against the cache
    with store.lock:
        rows = store.db.execute("SELECT id, path, name, size, mtime_ns, status FROM files").fetchall()
    by_path = {r[1]: r for r in rows}
    by_nsm: dict[tuple, list] = {}
    for r in rows:
        if r[1] not in found:  # only entries no longer at their old path (possibly moved)
            by_nsm.setdefault((r[2], r[3], r[4]), []).append(r)

    alive: set[int] = set()
    todo: list[tuple[int | None, str]] = []
    moved = 0
    with store.lock:
        for rel, (size, mtime, name) in found.items():
            r = by_path.get(rel)
            if r is not None:
                if r[3] == size and r[4] == mtime and (r[5] == "ok" or not retry_errors):
                    alive.add(r[0])
                else:
                    todo.append((r[0], rel))
                    alive.add(r[0])
                continue
            cands = by_nsm.get((name, size, mtime))
            if cands:
                r = cands.pop()
                store.db.execute("UPDATE files SET path=? WHERE id=?", (rel, r[0]))
                alive.add(r[0])
                moved += 1
                continue
            todo.append((None, rel))
        store.db.commit()

    # 3. Analyze new and changed files
    done = 0
    new_errors = 0
    progress("index", 0, len(todo), T("to_index", n=len(todo)))
    if todo:
        ctx = mp.get_context("spawn")
        ex = ProcessPoolExecutor(max_workers=max(1, workers), mp_context=ctx)
        try:
            paths = [str(root / rel) for _, rel in todo]
            it = ex.map(index_file, paths, chunksize=8)
            last_commit = time.time()
            for (fid, rel), info in zip(todo, it):
                size, mtime, name = found[rel]
                vals = [info.get(c) for c in FILE_COLS]
                with store.lock:
                    if fid is None:
                        cur = store.db.execute(
                            f"INSERT INTO files (path, name, size, mtime_ns, {', '.join(FILE_COLS)}) "
                            f"VALUES (?,?,?,?,{','.join('?' * len(FILE_COLS))})",
                            [rel, name, size, mtime] + vals)
                        alive.add(cur.lastrowid)
                    else:
                        store.db.execute(
                            f"UPDATE files SET name=?, size=?, mtime_ns=?, "
                            f"{', '.join(c + '=?' for c in FILE_COLS)} WHERE id=?",
                            [name, size, mtime] + vals + [fid])
                    if time.time() - last_commit > 2:
                        store.db.commit()
                        last_commit = time.time()
                if info["status"] != "ok":
                    new_errors += 1
                done += 1
                if done % 20 == 0 or done == len(todo):
                    progress("index", done, len(todo), rel)
                if stop.is_set():
                    break
        finally:
            with store.lock:
                store.db.commit()
            ex.shutdown(wait=not stop.is_set(), cancel_futures=True)
        if stop.is_set():
            return dict(stopped=True, indexed=done, total=len(found))

    # 4. Drop cache entries for files that no longer exist
    with store.lock:
        all_ids = [r[0] for r in store.db.execute("SELECT id FROM files")]
        gone = [(i,) for i in all_ids if i not in alive]
        store.db.executemany("DELETE FROM files WHERE id=?", gone)
        store.db.execute("DELETE FROM prefs WHERE id NOT IN (SELECT id FROM files)")
        store.db.commit()

    return dict(stopped=False, total=len(found), indexed=len(todo), reused=len(found) - len(todo),
                moved=moved, removed=len(gone), new_errors=new_errors, walk_errors=errors)


# ----------------------------------------------------------------------------
# 2) Find duplicates
# ----------------------------------------------------------------------------

@dataclass
class Member:
    id: int
    path: str
    size: int
    fmt: str
    width: int
    height: int
    quality: float | None
    make: str | None
    model: str | None
    software: str | None
    dto: str | None
    subsec: str | None
    genuine: bool
    has_gps: bool

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)

    @property
    def meta_score(self) -> int:
        return 2 * int(self.genuine) + int(bool(self.dto)) + int(self.has_gps)

    @property
    def camera(self) -> str:
        return " ".join(x for x in (self.make, self.model) if x) or "-"

    @property
    def taken(self) -> str:
        if not self.dto:
            return "-"
        return self.dto + (f".{self.subsec}" if self.subsec else "")


@dataclass
class Group:
    gid: int
    members: list[Member]
    status: str              # 'auto' | 'review'
    max_mae: float
    decisions: dict[int, str] = field(default_factory=dict)   # id -> 'keep' | 'q'
    approved: bool = False
    done: bool = False

    @property
    def keepers(self) -> list[Member]:
        return [m for m in self.members if self.decisions.get(m.id) == "keep"]

    @property
    def to_quarantine(self) -> list[Member]:
        return [m for m in self.members if self.decisions.get(m.id) == "q"]

    @property
    def saved_bytes(self) -> int:
        return sum(m.size for m in self.to_quarantine)

    def meta_donor(self) -> tuple[Member, Member] | None:
        """(kept file, donor) when the kept file has no capture time but a quarantined copy does."""
        ks = self.keepers
        if len(ks) != 1 or ks[0].dto:
            return None
        donors = [m for m in self.to_quarantine if m.dto]
        if not donors:
            return None
        return ks[0], max(donors, key=lambda m: (m.meta_score, m.pixels))


def _popcount(x: np.ndarray) -> np.ndarray:
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x)
    lut = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    return lut[x.view(np.uint8)].reshape(x.shape + (8,)).sum(-1)


def _capture_conflict(a: Member, b: Member) -> bool:
    """Both have original camera EXIF and different capture times -> different shots (e.g. a burst)."""
    if not (a.genuine and b.genuine and a.dto and b.dto):
        return False
    if a.model and b.model and a.model != b.model:
        return True
    if a.dto != b.dto:
        return True
    if a.subsec and b.subsec and a.subsec != b.subsec:
        return True
    return False


def _same_capture(a: Member, b: Member) -> bool:
    """Same capture time down to the sub-second -> copies of the same shutter release."""
    return bool(a.dto and b.dto and a.subsec and b.subsec
                and a.dto == b.dto and a.subsec == b.subsec)


def _maybe_burst(a: Member, b: Member) -> bool:
    """Same camera and same second but no sub-seconds: cannot tell a burst from a copy."""
    return bool(a.genuine and b.genuine and a.dto and b.dto and a.dto == b.dto
                and not (a.subsec and b.subsec))


def rank_members(members: list[Member]) -> Member:
    """Pick the copy to keep: resolution (within 3% counts as equal) -> metadata -> JPEG quality
    -> file size -> shorter path."""
    maxpix = max(m.pixels for m in members)
    pool = [m for m in members if m.pixels >= 0.97 * maxpix]
    return max(pool, key=lambda m: (m.meta_score, round((m.quality or 92) / 2),
                                    m.size, -len(m.path), m.path))


def apply_decisions(g: Group, prefs: dict[int, str]):
    keep_pref = [m for m in g.members if prefs.get(m.id) == "keep"]
    if keep_pref:
        g.decisions = {m.id: ("keep" if prefs.get(m.id) == "keep" else "q") for m in g.members}
    else:
        cands = [m for m in g.members if prefs.get(m.id) != "q"] or g.members
        best = rank_members(cands)
        g.decisions = {m.id: ("keep" if m.id == best.id else "q") for m in g.members}


def find_groups(store: Store, preset: str = DEFAULT_PRESET, burst_protect: bool = True,
                progress=_noop, stop: threading.Event | None = None) -> list[Group]:
    P = PRESETS[PRESET_ALIASES.get(preset, preset)]
    with store.lock:
        rows = store.db.execute(
            "SELECT id, path, size, fmt, width, height, quality, make, model, software, dto, subsec, "
            "genuine, has_gps, phash FROM files WHERE status='ok' AND phash IS NOT NULL").fetchall()
    n = len(rows)
    progress("find", 0, n, T("find_prep", n=n))
    if n < 2:
        return []
    mem = [Member(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11],
                  bool(r[12]), bool(r[13])) for r in rows]
    ph = np.frombuffer(b"".join(r[14] for r in rows), dtype=">u8").astype(np.uint64)
    ar = np.array([(m.width or 1) / max(1, m.height or 1) for m in mem])
    not_dup = store.not_dup_pairs()

    # Candidate pairs (pHash Hamming distance)
    cand = []
    B = 512
    for i0 in range(0, n, B):
        if stop and stop.is_set():
            return []
        blk = ph[i0:i0 + B]
        d = _popcount(blk[:, None] ^ ph[None, :])
        ii, jj = np.nonzero(d <= P["cand"])
        ii = ii + i0
        sel = jj > ii
        ii, jj = ii[sel], jj[sel]
        ok = np.abs(ar[ii] - ar[jj]) / np.maximum(ar[ii], ar[jj]) <= 0.03
        for i, j in zip(ii[ok], jj[ok]):
            cand.append((int(i), int(j)))
        progress("find", min(n, i0 + B), n, T("find_cand", i=min(n, i0 + B), n=n))

    # Thumbnail verification
    thumbs: dict[int, np.ndarray] = {}

    def thumb(i: int) -> np.ndarray:
        t = thumbs.get(i)
        if t is None:
            with store.lock:
                blob = store.db.execute("SELECT thumb FROM files WHERE id=?", (mem[i].id,)).fetchone()[0]
            t = np.frombuffer(zlib.decompress(blob), dtype=np.uint8).reshape(THUMB, THUMB).astype(np.float32)
            thumbs[i] = t
        return t

    def compare(i: int, j: int) -> tuple[float, float]:
        a, b = thumb(i), thumb(j)
        mae = float(np.abs(a - b).mean())
        za, zb = a - a.mean(), b - b.mean()
        den = float(np.sqrt((za * za).sum() * (zb * zb).sum()))
        corr = float((za * zb).sum() / den) if den > 1e-6 else (1.0 if mae < 1 else 0.0)
        return mae, corr

    edges = []   # (mae, i, j, strong)
    pair_info: dict[tuple[int, int], tuple[float, float]] = {}
    for k, (i, j) in enumerate(cand):
        if stop and stop.is_set():
            return []
        a, b = mem[i], mem[j]
        if (min(a.id, b.id), max(a.id, b.id)) in not_dup:
            continue
        if burst_protect and _capture_conflict(a, b):
            continue
        mae, corr = compare(i, j)
        pair_info[(i, j)] = (mae, corr)
        if mae <= P["mae"] and corr >= P["corr"]:
            strong = _same_capture(a, b) or (mae <= P["cert"] and not _maybe_burst(a, b))
            edges.append((mae, i, j, strong))
        if k % 2000 == 0:
            progress("find", k, len(cand), T("find_verify", i=k, n=len(cand)))

    # Constrained merging: most similar pairs first; every pair inside a group must satisfy the rules
    edges.sort()
    parent = {}
    groups: dict[int, list[int]] = {}
    strong_ok: dict[int, bool] = {}
    gmax: dict[int, float] = {}

    def find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    def pair_ok(i, j):
        a, b = mem[i], mem[j]
        if (min(a.id, b.id), max(a.id, b.id)) in not_dup:
            return False
        if burst_protect and _capture_conflict(a, b):
            return False
        if abs(ar[i] - ar[j]) / max(ar[i], ar[j]) > 0.03:
            return False
        key = (min(i, j), max(i, j))
        if key not in pair_info:
            if int(_popcount(np.array([ph[i] ^ ph[j]], dtype=np.uint64))[0]) > P["cand"]:
                return False
            pair_info[key] = compare(*key)
        mae, corr = pair_info[key]
        return mae <= P["mae"] * 1.3 and corr >= 1 - (1 - P["corr"]) * 1.5

    for mae, i, j, strong in edges:
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        gi, gj = groups.get(ri, [i]), groups.get(rj, [j])
        if len(gi) * len(gj) > 1 and not all(pair_ok(x, y) for x in gi for y in gj):
            continue
        parent[rj] = ri
        groups[ri] = gi + gj
        groups.pop(rj, None)
        strong_ok[ri] = strong_ok.get(ri, True) and strong_ok.pop(rj, True) and strong
        gmax[ri] = max(gmax.get(ri, 0.0), gmax.pop(rj, 0.0), mae)

    prefs = store.prefs()
    out = []
    for gid, (r, idx) in enumerate(sorted(groups.items(), key=lambda kv: mem[kv[1][0]].path)):
        ms = sorted((mem[i] for i in idx), key=lambda m: m.path)
        g = Group(gid=gid + 1, members=ms, status="auto" if strong_ok.get(r) else "review",
                  max_mae=gmax.get(r, 0.0))
        apply_decisions(g, prefs)
        g.approved = g.status == "auto"
        out.append(g)
    progress("find", n, n, T("find_done", n=len(out)))
    return out


# ----------------------------------------------------------------------------
# 3) Quarantine / restore
# ----------------------------------------------------------------------------

def _sidecars(p: Path) -> list[Path]:
    out = []
    try:
        for s in p.parent.iterdir():
            if s != p and s.stem == p.stem and s.suffix.lower() in SIDECAR_EXTS:
                out.append(s)
    except OSError:
        pass
    return out


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    i = 1
    while True:
        cand = dest.with_name(f"{dest.stem}__{i}{dest.suffix}")
        if not cand.exists():
            return cand
        i += 1


def _copy_meta(src: Path, dst: Path) -> str | None:
    exe = shutil.which("exiftool")
    if not exe:
        return T("no_exiftool")
    tags = ["-DateTimeOriginal", "-CreateDate", "-SubSecTimeOriginal", "-SubSecTimeDigitized",
            "-OffsetTimeOriginal", "-Make", "-Model", "-LensModel", "-ExposureTime", "-FNumber",
            "-ISO", "-FocalLength", "-GPS:all"]
    r = subprocess.run([exe, "-m", "-q", "-overwrite_original", "-TagsFromFile", str(src), *tags, str(dst)],
                       capture_output=True, text=True)
    return None if r.returncode == 0 else (r.stderr.strip() or f"exit {r.returncode}")


def quarantine(store: Store, groups: list[Group], copy_meta: bool = False, progress=_noop,
               stop: threading.Event | None = None) -> dict:
    root = store.root
    qroot = root / Q_DIR
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    todo = [g for g in groups if g.approved and not g.done and g.keepers and g.to_quarantine]
    total = sum(len(g.to_quarantine) for g in todo)
    moved = nbytes = meta_ok = 0
    errs = []
    log_path = qroot / "quarantine_log.csv"
    qroot.mkdir(parents=True, exist_ok=True)
    new_log = not log_path.exists()
    with open(log_path, "a", newline="", encoding="utf-8-sig") as lf:
        w = csv.writer(lf)
        if new_log:
            w.writerow(["time", "kind", "original", "quarantined_as", "kept_file"])
        for g in todo:
            if stop and stop.is_set():
                break
            keeper = g.keepers[0]
            kabs = store.abs(keeper.path)
            if not kabs.exists():
                errs.append(T("err_keeper_missing", gid=g.gid, path=keeper.path))
                continue
            if copy_meta:
                d = g.meta_donor()
                if d:
                    k, donor = d
                    bak = _unique(qroot / "_meta_backup" / k.path)
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(kabs, bak)
                    e = _copy_meta(store.abs(donor.path), kabs)
                    if e:
                        errs.append(T("err_meta", path=k.path, e=e))
                    else:
                        meta_ok += 1
                        with store.lock:
                            store.db.execute(
                                "INSERT INTO quarantine (kind, orig, qpath, keeper, ts) VALUES (?,?,?,?,?)",
                                ("meta_backup", k.path, store.rel(bak), donor.path, ts))
                        w.writerow([ts, "meta_backup", k.path, store.rel(bak), donor.path])
            for m in g.to_quarantine:
                src = store.abs(m.path)
                if not src.exists():
                    errs.append(T("err_missing", path=m.path))
                    continue
                dest = _unique(qroot / m.path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(src), str(dest))
                    sc_moved = []
                    for sc in _sidecars(src):
                        sd = _unique(dest.with_name(dest.stem + sc.suffix))
                        shutil.move(str(sc), str(sd))
                        sc_moved.append(f"{(Path(m.path).parent / sc.name).as_posix()}|{store.rel(sd)}")
                except OSError as e:
                    errs.append(T("err_move", path=m.path, e=e))
                    continue
                moved += 1
                nbytes += m.size
                with store.lock:
                    store.db.execute(
                        "INSERT INTO quarantine (kind, orig, qpath, keeper, ts, sidecars) VALUES (?,?,?,?,?,?)",
                        ("dup", m.path, store.rel(dest), keeper.path, ts, "\n".join(sc_moved)))
                    store.db.execute("DELETE FROM files WHERE id=?", (m.id,))
                w.writerow([ts, "dup", m.path, store.rel(dest), keeper.path])
                progress("quarantine", moved, total, m.path)
            g.done = True
            with store.lock:
                store.db.commit()
    # Kept files that received capture info get a new mtime, so the next scan re-analyzes them
    with store.lock:
        store.db.commit()
    return dict(moved=moved, bytes=nbytes, meta_copied=meta_ok, errors=errs)


def restore(store: Store, progress=_noop) -> dict:
    with store.lock:
        rows = store.db.execute(
            "SELECT qid, orig, qpath, sidecars FROM quarantine WHERE kind='dup' AND restored=0").fetchall()
    ok = 0
    errs = []
    for k, (qid, orig, qpath, sidecars) in enumerate(rows):
        src, dst = store.abs(qpath), store.abs(orig)
        if not src.exists():
            errs.append(T("err_q_missing", path=qpath))
            with store.lock:
                store.db.execute("UPDATE quarantine SET restored=2 WHERE qid=?", (qid,))
            continue
        if dst.exists():
            errs.append(T("err_dest_exists", path=orig))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        for line in (sidecars or "").splitlines():
            o, q = line.split("|", 1)
            if store.abs(q).exists() and not store.abs(o).exists():
                shutil.move(str(store.abs(q)), str(store.abs(o)))
        with store.lock:
            store.db.execute("UPDATE quarantine SET restored=1 WHERE qid=?", (qid,))
        ok += 1
        progress("restore", k + 1, len(rows), orig)
    with store.lock:
        store.db.commit()
    return dict(restored=ok, errors=errs)


def export_csv(groups: list[Group], path: Path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["group", "status", "approved", "action", "path", "width", "height", "size",
                    "jpeg_quality", "taken", "camera", "gps"])
        for g in groups:
            for m in g.members:
                w.writerow([g.gid, g.status, "Y" if g.approved else "", g.decisions.get(m.id), m.path,
                            m.width, m.height, m.size,
                            f"{m.quality:.0f}" if m.quality else "", m.taken, m.camera,
                            "Y" if m.has_gps else ""])


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------

def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _open_external(path: Path, select: bool = False):
    try:
        if _is_wsl():
            win = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True).stdout.strip()
            if select:
                subprocess.Popen(["explorer.exe", f"/select,{win}"])
            else:
                subprocess.Popen(["explorer.exe", win])
        elif sys.platform == "win32":
            os.startfile(str(path.parent if select else path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path.parent if select else path)])
    except Exception as e:
        print(T("open_failed", e=e), file=sys.stderr)


KOREAN_FONTS = ("Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "나눔고딕", "Malgun Gothic",
                "맑은 고딕", "NanumBarunGothic", "UnDotum", "Baekmuk Gulim", "Noto Sans CJK JP",
                "Noto Sans CJK SC", "Droid Sans Fallback")
KOREAN_MONO = ("Noto Sans Mono CJK KR", "D2Coding", "NanumGothicCoding", "나눔고딕코딩")


def _setup_korean_font(root) -> str | None:
    """Switch the Tk default fonts to one that contains Hangul (also needed for Korean file names).
    Returns None if no such font is installed."""
    from tkinter import font as tkfont

    fams = set(tkfont.families(root))
    pick = next((f for f in KOREAN_FONTS if f in fams), None)
    if pick is None:  # any font whose name suggests Korean coverage
        pick = next((f for f in sorted(fams)
                     if any(k in f for k in ("CJK KR", "Nanum", "나눔", "Malgun", "맑은", "Gothic KR"))), None)
    mono = next((f for f in KOREAN_MONO if f in fams), pick)
    if pick:
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont", "TkCaptionFont",
                     "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(family=pick)
            except Exception:
                pass
        tkfont.nametofont("TkFixedFont").configure(family=mono or pick, size=9)
    base = tkfont.nametofont("TkDefaultFont")
    tkfont.Font(root, name="PDHeadFont", family=base.actual("family"), size=11, weight="bold")
    return pick


def run_gui(initial_root: str | None = None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from PIL import ImageTk

    PRESET_KEYS = list(PRESETS)
    FILTER_KEYS = ["all", "auto", "review", "approved", "unapproved", "done"]
    LANG_KEYS = list(LANGS)

    class App:
        def __init__(self, master: tk.Tk):
            self.m = master
            master.geometry("1400x860")
            self.store: Store | None = None
            self.groups: list[Group] = []
            self.q: queue.Queue = queue.Queue()
            self.stop = threading.Event()
            self.busy = False
            self.cur: Group | None = None
            self.photo_cache: dict[str, ImageTk.PhotoImage] = {}
            self.kfont: str | None = None

            self.preset_key = DEFAULT_PRESET
            self.filter_key = "all"
            self.root_var = tk.StringVar(value=initial_root or "")
            self.burst_var = tk.BooleanVar(value=True)
            self.meta_var = tk.BooleanVar(value=bool(shutil.which("exiftool")))
            self.workers_var = tk.IntVar(value=max(1, min(6, (os.cpu_count() or 4) - 1)))
            self.status_var = tk.StringVar(value=T("status_start"))

            self._build()
            master.after(100, self._poll)
            if initial_root:
                self._open_store()

        # ---------- layout ----------
        def _build(self):
            m = self.m
            m.title(T("title"))
            top = ttk.Frame(m, padding=6)
            top.pack(fill="x")
            ttk.Label(top, text=T("folder")).pack(side="left")
            ttk.Entry(top, textvariable=self.root_var, width=70).pack(side="left", padx=4, fill="x", expand=True)
            ttk.Button(top, text=T("browse"), command=self._browse).pack(side="left")
            ttk.Button(top, text=T("open"), command=self._open_store).pack(side="left", padx=4)
            ttk.Label(top, text=T("language")).pack(side="left", padx=(12, 2))
            lcb = ttk.Combobox(top, values=[LANGS[k] for k in LANG_KEYS], width=8, state="readonly")
            lcb.current(LANG_KEYS.index(_LANG))
            lcb.pack(side="left")
            lcb.bind("<<ComboboxSelected>>", lambda e: self._set_language(LANG_KEYS[lcb.current()]))

            opt = ttk.Frame(m, padding=(6, 0))
            opt.pack(fill="x")
            ttk.Label(opt, text=T("sensitivity")).pack(side="left")
            pcb = ttk.Combobox(opt, values=[T("preset_" + k) for k in PRESET_KEYS], width=8, state="readonly")
            pcb.current(PRESET_KEYS.index(self.preset_key))
            self.preset_cb = pcb
            pcb.pack(side="left", padx=(2, 12))
            pcb.bind("<<ComboboxSelected>>", lambda e: setattr(self, "preset_key", PRESET_KEYS[pcb.current()]))
            ttk.Checkbutton(opt, text=T("burst"), variable=self.burst_var).pack(side="left", padx=4)
            ttk.Checkbutton(opt, text=T("copymeta"), variable=self.meta_var).pack(side="left", padx=12)
            ttk.Label(opt, text=T("workers")).pack(side="left", padx=(12, 2))
            ttk.Spinbox(opt, from_=1, to=32, textvariable=self.workers_var, width=4).pack(side="left")

            act = ttk.Frame(m, padding=6)
            act.pack(fill="x")
            self.btns = {}
            for key, txt, cmd in [
                ("scan", T("btn_scan"), self._scan),
                ("find", T("btn_find"), self._find),
                ("quar", T("btn_quar"), self._quarantine),
                ("restore", T("btn_restore"), self._restore),
                ("csv", T("btn_csv"), self._export),
            ]:
                b = ttk.Button(act, text=txt, command=cmd)
                b.pack(side="left", padx=3)
                self.btns[key] = b
            self.stop_btn = ttk.Button(act, text=T("btn_stop"), command=self.stop.set, state="disabled")
            self.stop_btn.pack(side="left", padx=12)
            self.db_lbl = ttk.Label(act, text="", foreground="#666")
            self.db_lbl.pack(side="right")

            pw = ttk.PanedWindow(m, orient="horizontal")
            pw.pack(fill="both", expand=True, padx=6)

            left = ttk.Frame(pw)
            pw.add(left, weight=1)
            fl = ttk.Frame(left)
            fl.pack(fill="x")
            ttk.Label(fl, text=T("view")).pack(side="left")
            fcb = ttk.Combobox(fl, values=[T("filter_" + k) for k in FILTER_KEYS], width=12, state="readonly")
            fcb.current(FILTER_KEYS.index(self.filter_key))
            fcb.pack(side="left", padx=4)

            def on_filter(e):
                self.filter_key = FILTER_KEYS[fcb.current()]
                self._fill_tree()

            fcb.bind("<<ComboboxSelected>>", on_filter)
            self.sum_lbl = ttk.Label(fl, text="")
            self.sum_lbl.pack(side="left", padx=8)

            cols = ("n", "status", "ok", "save", "keep")
            self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
            for c, wd in [("n", 50), ("status", 70), ("ok", 85), ("save", 90), ("keep", 260)]:
                self.tree.heading(c, text=T("col_" + c))
                self.tree.column(c, width=wd, anchor="w" if c == "keep" else "center")
            ys = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=ys.set)
            self.tree.pack(side="left", fill="both", expand=True)
            ys.pack(side="left", fill="y")
            self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_selected())
            self.tree.bind("<space>", lambda e: self._toggle_approve())
            self.tree.tag_configure("review", background="#fff4d6")
            self.tree.tag_configure("done", foreground="#999")

            lb = ttk.Frame(m, padding=(6, 2))
            lb.pack(fill="x")
            for txt, cmd in [(T("btn_toggle"), self._toggle_approve),
                             (T("btn_all_auto"), lambda: self._bulk("auto")),
                             (T("btn_all"), lambda: self._bulk("all")),
                             (T("btn_none"), lambda: self._bulk("none"))]:
                ttk.Button(lb, text=txt, command=cmd).pack(side="left", padx=3)

            right = ttk.Frame(pw)
            pw.add(right, weight=3)
            self.head_lbl = ttk.Label(right, text=T("head_empty"), font="PDHeadFont", padding=4)
            self.head_lbl.pack(fill="x")
            ttk.Label(right, foreground="#555", padding=(4, 0), text=T("hint")).pack(fill="x")
            self.canvas = tk.Canvas(right, highlightthickness=0, background="#f4f4f4")
            xs = ttk.Scrollbar(right, orient="horizontal", command=self.canvas.xview)
            self.canvas.configure(xscrollcommand=xs.set)
            xs.pack(side="bottom", fill="x")
            self.canvas.pack(fill="both", expand=True)
            self.cards = ttk.Frame(self.canvas)
            self.canvas.create_window((0, 0), window=self.cards, anchor="nw")
            self.cards.bind("<Configure>",
                            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
            for ev, d in (("<Button-4>", -1), ("<Button-5>", 1)):
                self.canvas.bind_all(ev, lambda e, d=d: self._wheel(e, d))
            self.canvas.bind_all("<MouseWheel>", lambda e: self._wheel(e, -1 if e.delta > 0 else 1))

            bot = ttk.Frame(m, padding=6)
            bot.pack(fill="x")
            self.pbar = ttk.Progressbar(bot, length=300, mode="determinate")
            self.pbar.pack(side="left")
            ttk.Label(bot, textvariable=self.status_var).pack(side="left", padx=8, fill="x", expand=True)
            self.log = tk.Text(m, height=6, wrap="none", font="TkFixedFont")
            self.log.pack(fill="x", padx=6, pady=(0, 6))

        def _set_language(self, code: str):
            """Switch the UI language in place, keeping all state."""
            if code == _LANG:
                return
            set_lang(code)
            _save_settings(lang=code)
            log_text = self.log.get("1.0", "end-1c")
            sel = self.tree.selection()
            for w in self.m.winfo_children():
                w.destroy()
            self._build()
            if log_text:
                self.log.insert("1.0", log_text if log_text.endswith("\n") else log_text + "\n")
                self.log.see("end")
            if self.store:
                self._open_store_stats()
            self._fill_tree()
            if sel and self.tree.exists(sel[0]):
                self.tree.selection_set(sel[0])
                self.tree.see(sel[0])
            if self.cur:
                self._show_group(self.cur)
            self.status_var.set(T("status_find_done") if self.groups else T("status_start"))
            if self.busy:
                self._set_busy(True)
            if not self.kfont and code == "ko":
                self._log(T("no_kfont"))

        def _wheel(self, e, d):
            w = e.widget
            if isinstance(w, str):
                return
            if str(w).startswith(str(self.canvas)):
                self.canvas.xview_scroll(d * 3, "units")

        # ---------- common ----------
        def _log(self, s: str):
            self.log.insert("end", time.strftime("[%H:%M:%S] ") + s + "\n")
            self.log.see("end")

        def _browse(self):
            d = filedialog.askdirectory(initialdir=self.root_var.get() or "/mnt")
            if d:
                self.root_var.set(d)
                self._open_store()

        def _open_store(self) -> bool:
            p = Path(self.root_var.get().strip()).expanduser()
            if not p.is_dir():
                messagebox.showerror(T("no_folder_title"), T("no_folder", p=p))
                return False
            if self.store and self.store.root == p.resolve():
                return True
            if self.store:
                self.store.close()
            self.store = Store(p)
            self.groups = []
            self.cur = None
            self._fill_tree()
            self._clear_cards()
            self._open_store_stats()
            self._log(T("log_open", p=p, ok=self.store.stats()["ok"]))
            return True

        def _open_store_stats(self):
            st = self.store.stats()
            self.db_lbl.config(text=T("db_label", name=self.store.path.name, ok=st["ok"],
                                      err=st["error"], q=st["quarantined"]))

        def _set_busy(self, b: bool):
            self.busy = b
            for btn in self.btns.values():
                btn.config(state="disabled" if b else "normal")
            self.stop_btn.config(state="normal" if b else "disabled")

        def _bg(self, kind: str, fn):
            if self.busy:
                return
            if not self._open_store():
                return
            self.stop.clear()
            self._set_busy(True)

            def prog(phase, done, total, text):
                self.q.put(("progress", phase, done, total, text))

            def target():
                try:
                    res = fn(prog)
                    self.q.put(("done", kind, res))
                except Exception:
                    self.q.put(("error", kind, traceback.format_exc()))

            threading.Thread(target=target, daemon=True).start()

        def _poll(self):
            last = None
            try:
                while True:
                    msg = self.q.get_nowait()
                    if msg[0] == "progress":
                        last = msg
                    else:
                        if last:
                            self._on_progress(*last[1:])
                            last = None
                        self._on_msg(msg)
            except queue.Empty:
                if last:
                    self._on_progress(*last[1:])
            self.m.after(150, self._poll)

        def _on_progress(self, phase, done, total, text):
            name = T("phase_" + phase)
            if total:
                self.pbar.config(mode="determinate", maximum=total, value=done)
                self.status_var.set(f"{name} {done}/{total}  {text}")
            else:
                self.pbar.config(mode="indeterminate")
                self.pbar.step(5)
                self.status_var.set(T("walk_progress", phase=name, n=done, text=text))

        def _on_msg(self, msg):
            kind = msg[1]
            self._set_busy(False)
            self.pbar.config(mode="determinate", value=0)
            if msg[0] == "error":
                self._log(msg[2])
                messagebox.showerror(T("error_title"), msg[2][-1500:])
                return
            res = msg[2]
            if kind == "scan":
                if res.get("stopped"):
                    self._log(T("scan_stopped"))
                else:
                    self._log(T("scan_done", total=res["total"], indexed=res["indexed"], reused=res["reused"],
                                moved=res["moved"], removed=res["removed"], errors=res["new_errors"]))
                    for e in res.get("walk_errors", [])[:20]:
                        self._log(T("walk_err", e=e))
                self._open_store_stats()
                self.status_var.set(T("status_scan_done"))
            elif kind == "find":
                self.groups = res
                self.cur = None
                self._fill_tree()
                n_auto = sum(g.status == "auto" for g in res)
                self._log(T("find_log", n=len(res), auto=n_auto, review=len(res) - n_auto))
                self.status_var.set(T("status_find_done"))
                kids = self.tree.get_children()
                if kids:
                    self.tree.selection_set(kids[0])
                    self.tree.focus(kids[0])
            elif kind == "quar":
                self._log(T("quar_log", n=res["moved"], size=human(res["bytes"]), meta=res["meta_copied"],
                            dir=self.store.root / Q_DIR))
                for e in res["errors"][:30]:
                    self._log("  " + e)
                self._fill_tree()
                self._open_store_stats()
                self.status_var.set(T("status_quar_done", n=res["moved"]))
            elif kind == "restore":
                self._log(T("restore_log", n=res["restored"]))
                for e in res["errors"][:30]:
                    self._log("  " + e)
                self._open_store_stats()
                self.status_var.set(T("status_restore_done"))

        # ---------- actions ----------
        def _scan(self):
            w = int(self.workers_var.get())
            self._log(T("log_scan_start"))
            self._bg("scan", lambda prog: scan(self.store, workers=w, progress=prog, stop=self.stop))

        def _find(self):
            pr, bp = self.preset_key, self.burst_var.get()
            self._log(T("log_find_start", p=T("preset_" + pr), b=T("on") if bp else T("off")))
            self._bg("find", lambda prog: find_groups(self.store, pr, bp, progress=prog, stop=self.stop))

        def _quarantine(self):
            todo = [g for g in self.groups if g.approved and not g.done]
            if not todo:
                messagebox.showinfo(T("quar_title"), T("quar_none"))
                return
            n = sum(len(g.to_quarantine) for g in todo)
            sz = sum(g.saved_bytes for g in todo)
            n_rev = sum(g.status == "review" for g in todo)
            msg = T("quar_confirm", g=len(todo), n=n, size=human(sz), dir=self.store.root / Q_DIR)
            if n_rev:
                msg += T("quar_confirm_review", n=n_rev)
            if self.meta_var.get():
                msg += T("quar_confirm_meta")
            if not messagebox.askokcancel(T("quar_title"), msg):
                return
            cm = self.meta_var.get()
            self._bg("quar", lambda prog: quarantine(self.store, self.groups, cm, progress=prog, stop=self.stop))

        def _restore(self):
            if not self._open_store():
                return
            n = self.store.stats()["quarantined"]
            if not n:
                messagebox.showinfo(T("restore_title"), T("restore_none"))
                return
            if messagebox.askokcancel(T("restore_title"), T("restore_confirm", n=n)):
                self._bg("restore", lambda prog: restore(self.store, progress=prog))

        def _export(self):
            if not self.groups:
                messagebox.showinfo("CSV", T("csv_need_find"))
                return
            p = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="photo_dup_groups.csv",
                                             initialdir=str(self.store.root))
            if p:
                export_csv(self.groups, Path(p))
                self._log(T("csv_saved", p=p))

        # ---------- group list ----------
        def _visible(self, g: Group) -> bool:
            return {"all": not g.done, "auto": g.status == "auto" and not g.done,
                    "review": g.status == "review" and not g.done, "approved": g.approved and not g.done,
                    "unapproved": not g.approved and not g.done, "done": g.done}[self.filter_key]

        def _row(self, g: Group):
            k = g.keepers[0].path if g.keepers else T("none")
            return (len(g.members), T("status_" + g.status), "✔" if g.approved else "",
                    human(g.saved_bytes), k)

        def _update_summary(self):
            live = [g for g in self.groups if not g.done]
            ap = [g for g in live if g.approved]
            self.sum_lbl.config(text=T("summary", g=len(live), a=len(ap),
                                       n=sum(len(g.to_quarantine) for g in ap),
                                       size=human(sum(g.saved_bytes for g in ap))))

        def _fill_tree(self):
            sel = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            for g in self.groups:
                if self._visible(g):
                    tags = ("done",) if g.done else (("review",) if g.status == "review" else ())
                    self.tree.insert("", "end", iid=str(g.gid), values=self._row(g), tags=tags)
            self._update_summary()
            if sel and self.tree.exists(sel[0]):
                self.tree.selection_set(sel[0])

        def _refresh_row(self, g: Group):
            if self.tree.exists(str(g.gid)):
                self.tree.item(str(g.gid), values=self._row(g))
            self._update_summary()

        def _group(self, gid) -> Group | None:
            for g in self.groups:
                if g.gid == int(gid):
                    return g
            return None

        def _toggle_approve(self):
            sel = self.tree.selection()
            if not sel:
                return
            g = self._group(sel[0])
            if g and not g.done:
                g.approved = not g.approved
                self._refresh_row(g)
                self._show_group(g)
            # move on to the next group
            nxt = self.tree.next(sel[0])
            if nxt:
                self.tree.selection_set(nxt)
                self.tree.focus(nxt)
                self.tree.see(nxt)
            return "break"

        def _bulk(self, mode: str):
            for g in self.groups:
                if g.done:
                    continue
                g.approved = {"auto": g.approved or g.status == "auto", "all": True, "none": False}[mode]
            self._fill_tree()
            if self.cur:
                self._show_group(self.cur)

        # ---------- group detail ----------
        def _clear_cards(self):
            for w in self.cards.winfo_children():
                w.destroy()

        def _show_selected(self):
            sel = self.tree.selection()
            if sel:
                g = self._group(sel[0])
                if g:
                    self._show_group(g)

        def _photo(self, rel: str, size=240):
            key = f"{rel}@{size}"
            ph = self.photo_cache.get(key)
            if ph:
                return ph
            p = self.store.abs(rel)
            try:
                with Image.open(p) as im:
                    if im.format == "JPEG":
                        im.draft("RGB", (size, size))
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    im.thumbnail((size, size))
                    ph = ImageTk.PhotoImage(im)
            except Exception:
                return None
            if len(self.photo_cache) > 200:
                self.photo_cache.clear()
            self.photo_cache[key] = ph
            return ph

        def _show_group(self, g: Group):
            self.cur = g
            self._clear_cards()
            self.head_lbl.config(
                text=T("head", gid=g.gid, n=len(g.members),
                       st=T("head_auto") if g.status == "auto" else T("head_review"),
                       ap=T("approved") if g.approved else T("not_approved"), mae=f"{g.max_mae:.1f}")
                + (T("head_done") if g.done else ""))
            best = rank_members(g.members)
            donor = g.meta_donor()
            for col, m in enumerate(g.members):
                keep = g.decisions.get(m.id) == "keep"
                card = tk.Frame(self.cards, bd=3, relief="solid", padx=6, pady=6,
                                background="#e3f5e1" if keep else "#fbe3e1",
                                highlightthickness=0)
                card.grid(row=0, column=col, padx=6, pady=6, sticky="n")
                ph = self._photo(m.path)
                il = tk.Label(card, image=ph, text="" if ph else T("no_preview"), width=30 if not ph else 0,
                              background=card["background"])
                il.image = ph
                il.pack()
                il.bind("<Double-Button-1>", lambda e, p=m.path: _open_external(self.store.abs(p)))
                q = f"{m.quality:.0f}" if m.quality else "-"
                lines = [
                    (T("card_keep") if keep else T("card_q")) + (T("card_best") if m.id == best.id else ""),
                    T("card_size", w=m.width, h=m.height, size=human(m.size), fmt=m.fmt, q=q),
                    T("card_taken", t=m.taken),
                    T("card_camera", c=m.camera) + ("" if m.genuine else T("card_noexif")),
                    T("card_gps", v=T("yes") if m.has_gps else T("no"))
                    + (f"   SW: {m.software[:20]}" if m.software else ""),
                    m.path,
                ]
                if donor and donor[0].id == m.id and self.meta_var.get():
                    lines.append(T("card_meta_note"))
                tk.Label(card, text="\n".join(lines), justify="left", anchor="w", wraplength=250,
                         background=card["background"]).pack(fill="x", pady=4)
                bf = tk.Frame(card, background=card["background"])
                bf.pack(fill="x")
                state = "disabled" if g.done else "normal"
                ttk.Button(bf, text=T("btn_to_q") if keep else T("btn_to_keep"), width=10, state=state,
                           command=lambda mm=m: self._toggle_keep(g, mm)).pack(side="left")
                ttk.Button(bf, text=T("btn_exclude"), width=9, state=state,
                           command=lambda mm=m: self._exclude(g, mm)).pack(side="left", padx=4)
                ttk.Button(bf, text=T("btn_folder"), width=6,
                           command=lambda p=m.path: _open_external(self.store.abs(p), True)).pack(side="left")
            self.canvas.xview_moveto(0)

        def _toggle_keep(self, g: Group, m: Member):
            if g.decisions.get(m.id) == "keep":
                if len(g.keepers) <= 1:
                    # quarantine this file and keep the next best one
                    others = [x for x in g.members if x.id != m.id]
                    nb = rank_members(others)
                    self.store.set_pref(m.id, "q")
                    self.store.set_pref(nb.id, "keep")
                else:
                    self.store.set_pref(m.id, "q")
            else:
                for x in g.members:
                    if g.decisions.get(x.id) == "keep":
                        self.store.set_pref(x.id, "q")
                self.store.set_pref(m.id, "keep")
            apply_decisions(g, self.store.prefs())
            self._refresh_row(g)
            self._show_group(g)

        def _exclude(self, g: Group, m: Member):
            for x in g.members:
                if x.id != m.id:
                    self.store.add_not_dup(m.id, x.id)
            self.store.set_pref(m.id, None)
            g.members = [x for x in g.members if x.id != m.id]
            if len(g.members) < 2:
                g.done = True
                self.groups.remove(g)
                self.cur = None
                self._fill_tree()
                self._clear_cards()
                self._log(T("excluded_log", gid=g.gid))
                return
            apply_decisions(g, self.store.prefs())
            self._refresh_row(g)
            self._show_group(g)

    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    kfont = _setup_korean_font(root)
    app = App(root)
    app.kfont = kfont
    if not kfont and _LANG == "ko":
        app._log(T("no_kfont"))
    root.mainloop()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _cli_progress(phase, done, total, text):
    if total:
        print(f"\r[{phase}] {done}/{total} {text[-60:]:<60}", end="", file=sys.stderr, flush=True)
        if done == total:
            print(file=sys.stderr)
    else:
        print(f"\r[{phase}] {done} {text[-60:]:<60}", end="", file=sys.stderr, flush=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    set_lang(_initial_lang(argv))

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--lang", choices=list(LANGS), default=argparse.SUPPRESS, help=T("help_lang"))
    ap = argparse.ArgumentParser(description=T("cli_desc"), parents=[common])
    sub = ap.add_subparsers(dest="cmd")
    g = sub.add_parser("gui", parents=[common])
    g.add_argument("root", nargs="?", help=T("help_root"))
    s = sub.add_parser("scan", parents=[common])
    s.add_argument("root", help=T("help_root"))
    s.add_argument("-j", "--workers", type=int, default=max(1, min(6, (os.cpu_count() or 4) - 1)),
                   help=T("help_workers"))
    s.add_argument("--retry-errors", action="store_true", help=T("help_retry"))
    for name in ("find", "quarantine"):
        p = sub.add_parser(name, parents=[common])
        p.add_argument("root", help=T("help_root"))
        p.add_argument("--preset", choices=list(PRESETS) + list(PRESET_ALIASES), default=DEFAULT_PRESET,
                       help=T("help_preset"))
        p.add_argument("--no-burst-protect", action="store_true", help=T("help_noburst"))
        p.add_argument("--csv", help=T("help_csv"))
        if name == "quarantine":
            p.add_argument("--include-review", action="store_true", help=T("help_review"))
            p.add_argument("--copy-meta", action="store_true", help=T("help_meta"))
    r = sub.add_parser("restore", parents=[common])
    r.add_argument("root", help=T("help_root"))
    args = ap.parse_args(argv)

    if args.cmd in (None, "gui"):
        run_gui(getattr(args, "root", None))
        return

    root = Path(args.root).expanduser()
    if not root.is_dir():
        sys.exit(T("not_dir", p=root))
    store = Store(root)
    try:
        if args.cmd == "scan":
            res = scan(store, workers=args.workers, progress=_cli_progress, retry_errors=args.retry_errors)
            print({k: v for k, v in res.items() if k != "walk_errors"})
            for e in res.get("walk_errors", [])[:20]:
                print(T("walk_err", e=e), file=sys.stderr)
        elif args.cmd in ("find", "quarantine"):
            groups = find_groups(store, args.preset, not args.no_burst_protect, progress=_cli_progress)
            n_auto = sum(g.status == "auto" for g in groups)
            print(T("cli_find", n=len(groups), auto=n_auto, review=len(groups) - n_auto,
                    q=sum(len(g.to_quarantine) for g in groups),
                    size=human(sum(g.saved_bytes for g in groups))))
            if args.csv:
                export_csv(groups, Path(args.csv))
                print("CSV:", args.csv)
            if args.cmd == "quarantine":
                for g in groups:
                    g.approved = g.status == "auto" or args.include_review
                res = quarantine(store, groups, args.copy_meta, progress=_cli_progress)
                print(T("cli_quar", n=res["moved"], size=human(res["bytes"]), meta=res["meta_copied"]))
                for e in res["errors"]:
                    print("  ", e, file=sys.stderr)
        elif args.cmd == "restore":
            res = restore(store, progress=_cli_progress)
            print(T("cli_restore", n=res["restored"]))
            for e in res["errors"]:
                print("  ", e, file=sys.stderr)
    finally:
        store.close()


if __name__ == "__main__":
    main()
