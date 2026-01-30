
import sys
import os
import asyncio
import json

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Load config (this handles .env loading via dotenv usually, but let's manual load if needed)
from dotenv import load_dotenv
load_dotenv()

import config
from utils.api_handlers import kakao

async def test_kakao_api():
    print(f"Testing with API Key: {config.KAKAO_API_KEY[:5]}... (masked)")
    
    # 1. Web Search Test
    print("\n--- 1. Web Search Test ('오늘의 날씨') ---")
    web_res = await kakao.search_web("오늘의 날씨", page_size=3)
    if web_res:
        print(f"Success! Found {len(web_res)} items.")
        print(json.dumps(web_res[0], indent=2, ensure_ascii=False))
    else:
        print("Web Search Failed or No Result.")

    # 2. Image Search Test
    print("\n--- 2. Image Search Test ('귀여운 고양이') ---")
    img_res = await kakao.search_image("귀여운 고양이", page_size=2)
    if img_res:
        print(f"Success! Found {len(img_res)} items.")
        print(json.dumps(img_res[0], indent=2, ensure_ascii=False))
    else:
        print("Image Search Failed.")

    # 3. Place Search Test
    print("\n--- 3. Place Search Test ('강남역 맛집') ---")
    # This returns pre-formatted string in current implementation
    place_str = await kakao.search_place_by_keyword("강남역 맛집", page_size=2)
    print(place_str)

    # 4. Blog Search Test
    print("\n--- 4. Blog Search Test ('강남역 맛집 후기') ---")
    blog_res = await kakao.search_blog("강남역 맛집 후기", page_size=2)
    if blog_res:
        print(f"Success! Found {len(blog_res)} items.")
        print(f"Sample: {blog_res[0].get('title')} ({blog_res[0].get('blogname')})")
    else:
        print("Blog Search Failed.")

    # 5. Video Search Test
    print("\n--- 5. Video Search Test ('아이유 라이브') ---")
    vclip_res = await kakao.search_vclip("아이유 라이브", page_size=2)
    if vclip_res:
        print(f"Success! Found {len(vclip_res)} items.")
        print(f"Sample: {vclip_res[0].get('title')} ({vclip_res[0].get('url')})")
    else:
        print("Video Search Failed.")

if __name__ == "__main__":
    asyncio.run(test_kakao_api())
