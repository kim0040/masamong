"""이미지 생성 로컬 선차단의 정상 문맥 회귀 테스트."""

from utils.constants import contains_nsfw


def test_image_safety_does_not_match_english_substrings():
    assert contains_nsfw(
        "A skilled illustrator designs a cocktail menu for a hotel."
    ) is False


def test_image_safety_allows_normal_adult_travel_and_photography_context():
    assert contains_nsfw("성인 2명이 머무는 숙소의 장시간 노출 사진") is False
    assert contains_nsfw("학교 성적표를 깔끔한 인포그래픽으로 만들어줘") is False


def test_image_safety_still_blocks_unambiguous_explicit_request():
    assert contains_nsfw("포르노 이미지를 만들어줘") is True
    assert contains_nsfw("create an explicit pornographic image") is True
