#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
마사몽 봇 설치 및 설정 스크립트
"""
import os
import sys
import subprocess
import json

def check_python_version():
    """Python 버전이 3.9 이상인지 확인"""
    if sys.version_info < (3, 9):
        print("❌ Python 3.9 이상이 필요합니다.")
        print(f"현재 버전: {sys.version}")
        return False
    print(f"✅ Python 버전 확인: {sys.version}")
    return True

def install_requirements():
    """requirements.txt 의존성 설치"""
    print("\n📦 의존성 설치 중...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        print("✅ 의존성 설치 완료")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ 의존성 설치 실패: {e}")
        return False

def setup_env_file():
    """환경변수 파일 설정"""
    print("\n⚙️ 환경변수 파일 설정...")
    
    if not os.path.exists('.env.example'):
        print("❌ .env.example 파일을 찾을 수 없습니다.")
        return False
    
    if not os.path.exists('.env'):
        print("📝 .env 파일을 생성합니다...")
        with open('.env.example', 'r', encoding='utf-8') as src:
            with open('.env', 'w', encoding='utf-8') as dst:
                dst.write(src.read())
        print("✅ .env 파일이 생성되었습니다.")
        print("⚠️  .env 파일을 편집하여 실제 API 키들을 입력해주세요!")
    else:
        print("✅ .env 파일이 이미 존재합니다.")
    
    return True

def setup_config_file():
    """설정 파일 설정 (emb_config.json, prompts.json)"""
    print("\n⚙️ 설정 파일 확인...")
    
    # emb_config.json 설정
    if not os.path.exists('emb_config.json'):
        if os.path.exists('emb_config.example.json'):
            print("📝 emb_config.json 파일을 생성합니다...")
            with open('emb_config.example.json', 'r', encoding='utf-8') as src:
                with open('emb_config.json', 'w', encoding='utf-8') as dst:
                    dst.write(src.read())
            print("✅ emb_config.json 파일이 생성되었습니다.")
        else:
            print("⚠️  emb_config.example.json 파일을 찾을 수 없습니다. 기본 설정을 사용합니다.")
    else:
        print("✅ emb_config.json 파일이 이미 존재합니다.")
    
    # prompts.json 설정
    if not os.path.exists('prompts.json'):
        if os.path.exists('prompts.example.json'):
            print("📝 prompts.json 파일을 생성합니다...")
            with open('prompts.example.json', 'r', encoding='utf-8') as src:
                with open('prompts.json', 'w', encoding='utf-8') as dst:
                    dst.write(src.read())
            print("✅ prompts.json 파일이 생성되었습니다.")
        else:
            print("⚠️  prompts.example.json 파일을 찾을 수 없습니다. 기본 설정을 사용합니다.")
    else:
        print("✅ prompts.json 파일이 이미 존재합니다.")
    
    return True

def initialize_database():
    """데이터베이스 초기화"""
    print("\n🗃️ 데이터베이스 초기화...")
    try:
        subprocess.check_call([sys.executable, "database/init_db.py"])
        print("✅ 데이터베이스 초기화 완료")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ 데이터베이스 초기화 실패: {e}")
        return False

def test_configuration():
    """설정 테스트"""
    print("\n🧪 설정 테스트...")
    try:
        # config 모듈 테스트
        import config
        print("✅ config.py 로드 성공")
        
        # 필수 환경변수 확인
        if not config.TOKEN:
            print("⚠️  DISCORD_BOT_TOKEN이 설정되지 않았습니다.")
        else:
            print("✅ Discord 봇 토큰 확인")
            
        if not config.COMETAPI_KEY:
            print("⚠️  COMETAPI_KEY가 설정되지 않았습니다. (선택사항)")
        else:
            print("✅ CometAPI 키 확인")
        
        # DB 설정 확인
        print(f"✅ DB 백엔드: {config.DB_BACKEND}")
        
        return True
    except Exception as e:
        print(f"❌ 설정 테스트 실패: {e}")
        return False

def main():
    """봇 설치 및 설정 초기화 스크립트의 진입점입니다."""
    print("🤖 마사몽 봇 설치 프로그램")
    print("=" * 50)
    
    if not check_python_version():
        return False
    
    if not install_requirements():
        return False
    
    if not setup_env_file():
        return False
    
    if not setup_config_file():
        return False
    
    if not initialize_database():
        return False
    
    if not test_configuration():
        return False
    
    print("\n🎉 설치 완료!")
    print("\n다음 단계:")
    print("1. .env 파일을 편집하여 실제 API 키들을 입력하세요")
    print("2. python main.py 명령어로 봇을 실행하세요")
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
