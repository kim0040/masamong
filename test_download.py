
import traceback
from huggingface_hub import hf_hub_download

print('--- Starting download test ---')
try:
    file_path = hf_hub_download(repo_id='BM-K/KoSimCSE-roberta', filename='config.json')
    print('--- SUCCESS ---')
    print(f'Downloaded to: {file_path}')
except Exception as e:
    print('--- ERROR ---')
    print(f'An exception occurred: {type(e).__name__}: {e}')
    traceback.print_exc()
print('--- Test finished ---')
