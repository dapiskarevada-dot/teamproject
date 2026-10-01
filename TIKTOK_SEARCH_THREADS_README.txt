TikTok SEARCH -> POSTS -> COMMENTS -> REPLIES

Files:
  collect_tiktok_threads.py
  collect_tiktok_search_threads.py

Keep both files in the same pytok_research directory.

1) No-network compatibility check:
   python .\collect_tiktok_search_threads.py --check

2) Small safe search test:
   python .\collect_tiktok_search_threads.py --query "умскул" --search-count 5 --comments 10

3) Search only:
   python .\collect_tiktok_search_threads.py --query "умскул" --search-count 20 --search-only

4) Several queries:
   python .\collect_tiktok_search_threads.py --query "умскул" --query "егэленд" --search-count 10 --comments 20

5) Queries from file:
   python .\collect_tiktok_search_threads.py --queries-file .\queries.txt --search-count 20 --comments 50

Defaults:
  request delay: 4 s
  wait after opening comments: min 5 s / max 15 s
  delay between reply roots: 4 s
  delay between posts: 8 s
  delay between search queries: 10 s
  automatic retries: OFF

Notes:
- Search results are saved before comment collection.
- Duplicate posts across queries are deduplicated by post_id.
- Query/rank provenance is preserved.
- Replies are fetched only for top-level roots that are actually saved.
- TikTok search is a retrieval mechanism, not an exhaustive/random sample.
