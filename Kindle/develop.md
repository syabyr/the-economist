prefetch_issue.py

1.根据issue-date,通过graphql获取weekly的edition,保存在cache_dir/editions,里面包含 weekly edition
2.将edition的内容,合并封装到manifest.json里
3.根据此edition里的urls,去graphql查询文章,并保存在以文件名hash的articles目录
4.从article里找到插图的链接,保存起来,供后续统一下载(插图的质量可以选择吗?)
5.下载所有插图


translate_issue_cache.py

1.根据cache目录,以及是否开启翻译,从articles的文章片段里获取内容并翻译,通过hash进行缓存



build-issue.sh


