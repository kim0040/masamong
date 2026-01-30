
import sys
import os
import asyncio
import json

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
load_dotenv()

from utils.api_handlers import kakao

async def show_masamong_vision(query):
    print(f"🔎 마사몽이 '{query}'에 대해 읽게 될 원본 데이터 시뮬레이션...\n")
    print("="*60)
    
    # Simulate cogs/tools_cog.py logic
    web_task = kakao.search_web(query, page_size=5)
    blog_task = kakao.search_blog(query, page_size=3)
    vclip_task = kakao.search_vclip(query, page_size=3)
    
    results = await asyncio.gather(web_task, blog_task, vclip_task, return_exceptions=True)
    web_res, blog_res, vclip_res = results
    
    output_parts = []

    # 1. Web Results
    if isinstance(web_res, list) and web_res:
        formatted = [f"{i}. {r.get('title', '제목 없음').replace('<b>','').replace('</b>','')}\n   - {r.get('contents', '내용 없음').replace('<b>','').replace('</b>','')}" for i, r in enumerate(web_res, 1)]
        output_parts.append(f"## 🌐 웹 검색 결과:\n" + "\n".join(formatted))
    
    # 2. Blog Results
    if isinstance(blog_res, list) and blog_res:
        formatted = [f"{i}. [블로그] {r.get('title', '').replace('<b>','').replace('</b>','')}\n   - {r.get('blogname', '')}: {r.get('contents', '').replace('<b>','').replace('</b>','')}" for i, r in enumerate(blog_res, 1)]
        output_parts.append(f"## 📝 블로그/후기 검색 결과:\n" + "\n".join(formatted))

    # 3. Video Results
    if isinstance(vclip_res, list) and vclip_res:
        formatted = [f"{i}. [영상] {r.get('title', '').replace('<b>','').replace('</b>','')}\n   - {r.get('author', '저자')}: {r.get('url')}" for i, r in enumerate(vclip_res, 1)]
        output_parts.append(f"## 🎬 동영상 검색 결과:\n" + "\n".join(formatted))

    final_output = f"'{query}'에 대한 통합 검색 결과 (Kakao):\n\n" + "\n\n".join(output_parts)
    
    print(final_output)
    print("="*60)
    print(f"\n[System] 총 데이터 길이: {len(final_output)}자")

if __name__ == "__main__":
    asyncio.run(show_masamong_vision("노란봉투법"))
