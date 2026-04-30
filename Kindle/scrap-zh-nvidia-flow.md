# scrap-zh-nvidia.sh 流程梳理

## 1. 入口脚本做了什么

入口是 [scrap-zh-nvidia.sh](scrap-zh-nvidia.sh)。

它主要做三件事：
1. 校验翻译 API Key（支持 NVIDIA_API_KEY 或 ECONOMIST_TRANSLATE_API_KEY）
2. 导出中英双语翻译相关环境变量（provider/model/source/target）
3. 调用 [scrap.sh](scrap.sh) 进入主流程

因此它本身不做抓取和出书，核心作用是“开启并配置翻译模式”。

## 2. 主流程总览

主流程在 [scrap.sh](scrap.sh)：
1. ./download-issue.sh YYYY-MM-DD ISSUE_ID
2. 如果 ECONOMIST_TRANSLATE_ZH=1，则 ./translate-zh-nvidia.sh YYYY-MM-DD ISSUE_ID
3. ./build-issue.sh YYYY-MM-DD ISSUE_ID

这也是电子书生成的三阶段流水线：
- 下载并缓存原始内容
- 预翻译并缓存中文片段
- 读取缓存并生成 mobi/pdf

## 3. 阶段一：下载与缓存

由 [download-issue.sh](download-issue.sh) 和 [prefetch_issue.py](prefetch_issue.py) 完成。

关键动作：
1. 创建缓存目录 economist_content_cache/<issue_date>-<issue_id>
2. 调 GraphQL FindEditionByDate 拿到当期目录和文章 URL
3. 遍历文章 URL 调 ArticleDeeplinkQuery 拉取全文 JSON
4. 递归提取封面图、文中图片 URL，并下载图片到 images 目录
5. 生成 manifest.json（包含期号、标题、文章列表）

输出结果：
- editions/*.json（期刊元数据）
- articles/*.json（文章详情）
- images/*（封面和文内图）
- manifest.json（本期清单）

## 4. 阶段二：预翻译与翻译缓存

由 [translate-zh-nvidia.sh](translate-zh-nvidia.sh) 和 [translate_issue_cache.py](translate_issue_cache.py) 完成。

关键动作：
1. 校验缓存目录存在（必须先执行下载阶段）
2. 设置 ECONOMIST_CACHE_ONLY=1，确保后续只读本地缓存
3. 遍历 articles/*.json，提取可翻译片段（标题、正文段落、图片说明、列表、信息框等）
4. 调 NVIDIA 兼容 chat/completions 接口翻译 HTML 片段
5. 把翻译结果按 hash 写入 translate.json

特性：
- 翻译有重试与延迟退避
- 同片段命中缓存后不重复调用模型
- 保留原 HTML 内联标签

## 5. 阶段三：构建电子书

由 [build-issue.sh](build-issue.sh) + [economist.recipe](economist.recipe) + calibre 的 ebook-convert 完成。

关键动作：
1. 从 editions 缓存中定位封面 URL，对应到本地 images 文件
2. 设置 ECONOMIST_CONTENT_CACHE_DIR 与 ECONOMIST_CACHE_ONLY=1，recipe 只读缓存
3. 若存在 translate.json，则开启 ECONOMIST_TRANSLATE_ZH=1 并让 recipe 读取翻译缓存
4. 复制 economist.recipe 为 TheEconomist-<date>-<id>.recipe，并写入 edition_date
5. ebook-convert 生成 mobi
6. ebook-convert 生成 pdf
7. 清理临时 recipe，移动输出文件到目标目录

输出结果：
- <year>/TheEconomist-<date>-<id>.mobi
- pdf/<year>/TheEconomist-<date>-<id>.pdf

## 6. recipe 中英双语渲染机制

在 [economist.recipe](economist.recipe) 中：
1. BilingualTranslator 负责翻译调用与 translate.json 缓存
2. render_bilingual_inline/render_bilingual_block 负责“英文在上、中文在下”渲染
3. process_web_node 处理段落、标题、引文、图片说明、列表、信息框等节点
4. 启用 ECONOMIST_CACHE_ONLY=1 时，内容来源于本地缓存，不再实时抓网

所以最终书里的双语效果，本质是：
- 内容抓取阶段得到英文原文 JSON
- 翻译阶段得到片段级中文缓存
- 构建阶段 recipe 将两者合并排版

## 7. 全流程图

flowchart TD
    A[运行 scrap-zh-nvidia.sh date id] --> B{API Key 是否存在}
    B -- 否 --> Z1[退出: 提示设置 NVIDIA_API_KEY]
    B -- 是 --> C[导出翻译环境变量]
    C --> D[调用 scrap.sh]

    D --> E[download-issue.sh]
    E --> F[创建 economist_content_cache/date-id]
    F --> G[prefetch_issue.py]
    G --> H[FindEditionByDate 获取当期目录]
    H --> I[遍历文章 URL 拉取 Article JSON]
    I --> J[提取并下载封面/文内图片]
    J --> K[写 manifest.json + editions/articles/images 缓存]

    K --> L{ECONOMIST_TRANSLATE_ZH == 1}
    L -- 否 --> R[build-issue.sh]
    L -- 是 --> M[translate-zh-nvidia.sh]
    M --> N{缓存目录是否存在}
    N -- 否 --> Z2[退出: 提示先 download-issue.sh]
    N -- 是 --> O[translate_issue_cache.py 遍历 articles]
    O --> P[提取可翻译 HTML 片段]
    P --> Q[调用 NVIDIA chat/completions 翻译]
    Q --> Q2[写入 translate.json]
    Q2 --> R

    R --> S[定位本地 cover 并设置 recipe 环境]
    S --> T[根据 date 生成临时 recipe]
    T --> U[ebook-convert 生成 mobi]
    U --> V[ebook-convert 生成 pdf]
    V --> W[移动产物到 year 和 pdf/year]
    W --> X[完成: 双语电子书生成]
