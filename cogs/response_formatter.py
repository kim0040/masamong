# -*- coding: utf-8 -*-
import discord
from datetime import datetime
import json

def _create_base_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
    """기본 임베드 형식을 생성합니다."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now()
    )
    embed.set_footer(text="Powered by Masamong AI Agent")
    return embed

def _format_stock_embed(user_query: str, execution_context: dict, synthesized_response: str) -> discord.Embed:
    """주식 정보에 대한 임베드를 생성합니다."""
    try:
        stock_data = execution_context['step_1_result']['result']
        change = stock_data.get('change', 0)
        color = discord.Color.green() if change >= 0 else discord.Color.red()

        title = f"📈 주식 시세: {stock_data.get('name', user_query)}"
        embed = _create_base_embed(title, synthesized_response, color)

        embed.add_field(name="현재가", value=f"{stock_data.get('price', 'N/A'):,}", inline=True)
        embed.add_field(name="전일 대비", value=f"{change:,} ({stock_data.get('change_rate', 'N/A')}%)", inline=True)

        # 만약 환율 정보도 있다면 추가
        if 'step_2_result' in execution_context and execution_context['step_2_result']['tool'] == 'get_krw_exchange_rate':
            rate_data = execution_context['step_2_result']['result']
            krw_price = stock_data.get('price', 0) * rate_data.get('rate', 0)
            embed.add_field(name="원화 환산", value=f"약 {krw_price:,.0f}원", inline=False)

        return embed
    except (KeyError, TypeError, IndexError):
        # 데이터 구조가 예상과 다를 경우, 일반 임베드로 대체
        return _create_base_embed(user_query, synthesized_response, discord.Color.blue())


def _format_lol_match_embed(user_query: str, execution_context: dict, synthesized_response: str) -> discord.Embed:
    """LoL 전적 정보에 대한 임베드를 생성합니다."""
    try:
        match_data = execution_context['step_1_result']['result']['matches'][0]
        color = discord.Color.green() if match_data.get('win') else discord.Color.red()

        title = f"🎮 LoL 최근 전적: {match_data.get('summoner_name')}"
        embed = _create_base_embed(title, synthesized_response, color)

        kda = f"{match_data.get('kills', 0)}/{match_data.get('deaths', 0)}/{match_data.get('assists', 0)}"
        embed.add_field(name="KDA", value=kda, inline=True)
        embed.add_field(name="챔피언", value=match_data.get('champion_name', 'N/A'), inline=True)

        # 아이템 정보는 간단히 표시
        items = [str(i) for i in match_data.get('items', []) if i != 0]
        embed.add_field(name="아이템", value=", ".join(items) if items else "없음", inline=False)

        return embed
    except (KeyError, TypeError, IndexError):
        return _create_base_embed(user_query, synthesized_response, discord.Color.blue())

def _format_place_embed(user_query: str, execution_context: dict, synthesized_response: str) -> discord.Embed:
    """장소 검색 결과에 대한 임베드를 생성합니다."""
    try:
        places = execution_context['step_1_result']['result']['places']

        title = f"🗺️ '{user_query}' 장소 검색 결과"
        embed = _create_base_embed(title, synthesized_response, discord.Color.dark_green())

        for i, place in enumerate(places[:3]): # 최대 3개까지 표시
            place_name = place.get('place_name', '이름 없음')
            category = place.get('category_name', '카테고리 없음')
            address = place.get('road_address_name', '주소 없음')
            url = place.get('place_url', '')

            embed.add_field(
                name=f"{i+1}. {place_name}",
                value=f"**카테고리:** {category}\n**주소:** {address}\n[카카오맵에서 보기]({url})",
                inline=False
            )
        return embed
    except (KeyError, TypeError, IndexError):
        return _create_base_embed(user_query, synthesized_response, discord.Color.blue())


def _format_error_embed(user_query: str, execution_context: dict) -> discord.Embed:
    """오류 발생 시 사용자에게 보여줄 임베드를 생성합니다."""
    error_message = "알 수 없는 오류가 발생했습니다."
    # 컨텍스트에서 마지막 오류 메시지를 찾음
    for step_result in reversed(execution_context.values()):
        if "error" in step_result:
            error_message = step_result["error"]
            break

    embed = _create_base_embed(
        title=f"😥 이런, 문제가 발생했네!",
        description=f"요청하신 '{user_query}' 작업을 처리하는 중에 문제가 발생했어요.\n\n**오류 내용:**\n`{error_message}`",
        color=discord.Color.orange()
    )
    return embed

def format_final_response(user_query: str, execution_context: dict, synthesized_response: str) -> discord.Embed | str:
    """
    실행 컨텍스트를 분석하여 적절한 포맷의 임베드를 반환하는 디스패처 함수.
    적절한 임베드가 없으면 원본 합성 응답 문자열을 반환.
    """
    if not execution_context:
        return synthesized_response

    # 오류가 있는지 먼저 확인
    if any(isinstance(step, dict) and "error" in step for step in execution_context.values()):
        return _format_error_embed(user_query, execution_context)

    # 사용된 도구 목록을 확인
    tools_used = {step.get('tool') for step in execution_context.values() if isinstance(step, dict) and step.get('tool')}

    if "search_for_place" in tools_used:
        return _format_place_embed(user_query, execution_context, synthesized_response)
    if "get_stock_price" in tools_used:
        return _format_stock_embed(user_query, execution_context, synthesized_response)
    if "get_lol_match_history" in tools_used:
        return _format_lol_match_embed(user_query, execution_context, synthesized_response)

    # 다른 특별한 포맷이 없는 경우, 일반적인 임베드 사용
    first_step = execution_context.get("step_1", {})
    if first_step.get("tool") == "general_chat":
        # 일반 채팅은 임베드 없이 텍스트로만 응답
        return synthesized_response
    else:
        # 도구를 사용했지만 특별한 포맷이 없는 경우
        return _create_base_embed(user_query, synthesized_response, discord.Color.default())
