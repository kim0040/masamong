# -*- coding: utf-8 -*-
"""초기 위치 좌표 데이터와 CSV 기반 로딩 유틸리티 제공."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# 기상청 격자 좌표 (nx, ny) 데이터
# CSV 파일을 찾지 못했을 때 사용할 최소 기본값입니다.
# (광양은 기본 위치이므로 반드시 포함합니다.)
LOCATION_DATA = [
    {'name': '광양', 'nx': 73, 'ny': 70},
    {'name': '서울', 'nx': 60, 'ny': 127},
    {'name': '부산', 'nx': 98, 'ny': 76},
    {'name': '대구', 'nx': 89, 'ny': 90},
    {'name': '인천', 'nx': 55, 'ny': 124},
    {'name': '대전', 'nx': 67, 'ny': 100},
    {'name': '광주', 'nx': 58, 'ny': 74},
    {'name': '울산', 'nx': 102, 'ny': 84},
    {'name': '제주', 'nx': 52, 'ny': 38},
]

CSV_FILENAME = "동네예보지점좌표(위경도)_202510.csv"
SHORT_NAME_MAP = {
    '서울특별시': ['서울'],
    '부산광역시': ['부산'],
    '대구광역시': ['대구'],
    '인천광역시': ['인천'],
    '광주광역시': ['광주'],
    '대전광역시': ['대전'],
    '울산광역시': ['울산'],
    '세종특별자치시': ['세종'],
    '제주특별자치도': ['제주'],
    '강원특별자치도': ['강원'],
    '경기도': ['경기'],
    '경상북도': ['경북'],
    '경상남도': ['경남'],
    '전라북도': ['전북'],
    '전라남도': ['전남'],
    '충청북도': ['충북'],
    '충청남도': ['충남'],
}


def _normalize_name(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


def _expand_level_aliases(level: str) -> Iterable[str]:
    level = _normalize_name(level)
    if not level:
        return []
    aliases = [level]
    if level in SHORT_NAME_MAP:
        aliases.extend(SHORT_NAME_MAP[level])
    elif level.endswith('특별시') and len(level) > 3:
        aliases.append(level[:-3])
    elif level.endswith('광역시') and len(level) > 3:
        aliases.append(level[:-3])
    elif level.endswith('특별자치시') and len(level) > 5:
        aliases.append(level[:-5])
    elif level.endswith('특별자치도') and len(level) > 5:
        aliases.append(level[:-5])
    elif level.endswith('도') and len(level) > 1:
        aliases.append(level[:-1])
    elif level.endswith('시') and len(level) > 1:
        aliases.append(level[:-1])
    elif level.endswith('군') and len(level) > 1:
        aliases.append(level[:-1])
    return aliases


def _collect_candidate_names(level1: str, level2: str, level3: str) -> set[str]:
    names: set[str] = set()
    l1_aliases = list(_expand_level_aliases(level1))
    l2_aliases = list(_expand_level_aliases(level2))
    l3_aliases = [_normalize_name(level3)] if _normalize_name(level3) else []

    # 단일 레벨 조합
    names.update(alias for alias in l1_aliases if alias)
    if not _normalize_name(level3):
        names.update(alias for alias in l2_aliases if alias and len(alias) > 1)
    names.update(alias for alias in l3_aliases if alias and len(alias) > 1)

    # 결합된 이름 (상세 → 광역 순)
    def _join(*parts: str) -> str:
        joined = " ".join(p for p in parts if p)
        return _normalize_name(joined)

    combos = {
        _join(level1, level2, level3),
        _join(level1, level2),
        _join(level2, level3),
        _join(level1, level3),
    }
    names.update(name for name in combos if name)

    # 광역명+세부명 조합 (예: "서울 청운효자동")
    for l1 in l1_aliases:
        for l2 in l2_aliases:
            names.add(_join(l1, l2))
        for l3 in l3_aliases:
            names.add(_join(l1, l3))
    for l2 in l2_aliases:
        for l3 in l3_aliases:
            names.add(_join(l2, l3))

    return {n for n in names if n}


def load_locations_from_csv(csv_path: str | Path | None = None) -> list[dict[str, int]]:
    """CSV 파일에서 기상청 격자 좌표를 읽어 DB 시딩용 리스트를 반환합니다."""

    if csv_path is None:
        csv_path = Path(__file__).resolve().parent.parent / CSV_FILENAME
    else:
        csv_path = Path(csv_path)

    if not csv_path.exists():
        logger.warning("Location CSV not found: %s", csv_path)
        return []

    entries: dict[str, tuple[int, int]] = {}

    try:
        with csv_path.open('r', encoding='utf-8-sig') as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                try:
                    nx = int(float(row.get('격자 X', '0')))
                    ny = int(float(row.get('격자 Y', '0')))
                except ValueError:
                    continue

                level1 = row.get('1단계', '')
                level2 = row.get('2단계', '')
                level3 = row.get('3단계', '')
                if not nx or not ny:
                    continue
                for name in _collect_candidate_names(level1, level2, level3):
                    if name in entries:
                        # 동일 이름인데 좌표가 다르면 구체적인(긴) 이름만 유지합니다.
                        existing = entries[name]
                        if existing != (nx, ny):
                            if len(name.split()) == 1:
                                continue
                            logger.debug("Duplicate location name '%s' with differing coords detected. Keeping first entry.", name)
                            continue
                    entries.setdefault(name, (nx, ny))
    except Exception as exc:
        logger.error("Failed to load location CSV '%s': %s", csv_path, exc, exc_info=True)
        return []

    if not entries:
        logger.warning("No location entries parsed from CSV: %s", csv_path)
        return []

    logger.info("Loaded %d location entries from CSV.", len(entries))
    return [{'name': name, 'nx': coords[0], 'ny': coords[1]} for name, coords in entries.items()]
