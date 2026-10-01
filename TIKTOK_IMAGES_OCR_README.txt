TikTok IMAGE COLLECTION + PADDLE OCR
====================================

Two separate scripts on purpose:

1) collect_tiktok_images.py
   TikTok/browser side. Opens posts with the existing PyTok research account,
   finds large post image assets, downloads original bytes, deduplicates them by
   SHA256 and writes images_manifest.json.

2) paddle_ocr_folder.py
   Local OCR side. Never opens TikTok. Reads already-saved images and runs
   PaddleOCR.

WHY SEPARATE THEM
-----------------
The raw image is the provenance/evidence layer. You can re-run a better OCR/VLM
later without touching TikTok again.

PYTOK ENVIRONMENT
-----------------
Put collect_tiktok_images.py in the existing pytok_research directory.

Check without TikTok request:
    python .\collect_tiktok_images.py --check

One post:
    python .\collect_tiktok_images.py --url "https://www.tiktok.com/@user/photo/123"

Several posts:
    python .\collect_tiktok_images.py --url "URL1" --url "URL2"

File with one URL per line:
    python .\collect_tiktok_images.py --urls-file .\urls.txt

Default output:
    cases\tiktok_media\raw\posts\<post_id>\images\
    cases\tiktok_media\raw\posts\<post_id>\images_manifest.json

OCR ENVIRONMENT
---------------
Recommended separate environment:

    deactivate
    py -3.11 -m venv .venv-ocr
    .\.venv-ocr\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    python -m pip install paddlepaddle==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
    python -m pip install paddleocr

One post:
    python .\paddle_ocr_folder.py .\cases\tiktok_media\raw\posts\<post_id>\images

All saved posts:
    python .\paddle_ocr_folder.py .\cases\tiktok_media\raw\posts --recursive

Outputs:
    ocr_paddle\ocr_results.csv
    ocr_paddle\ocr_results.json
    ocr_paddle\ocr_results.txt
    plus one *.ocr.json per image

NOTES
-----
- TikTok changes markup; no scraper can honestly promise "works forever on every
  post". This collector uses rendered DOM + hydration JSON and deduplicates the
  downloaded assets to be resilient to common layouts.
- No automatic TikTok retries.
- OCR is deliberately separate from collection.
