


from bs4 import BeautifulSoup
import requests
import json
import os
import urllib.request
from html5_parser import parse
from lxml import etree
from collections import defaultdict
from mdutils.mdutils import MdUtils




def parse_index(edition_date):

    if edition_date:
        url = 'https://www.economist.com/weeklyedition/' + edition_date
        #self.timefmt = ' [' + edition_date + ']'
    else:
        url = 'https://www.economist.com/printedition'
    html_doc = requests.get(url);
    #print(html_doc.content);
    soup = BeautifulSoup(html_doc.content, 'html.parser');
    #print(soup)

    result = economist_parse_index(soup);
    return (result)


def economist_parse_index(soup):
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if script_tag is not None:
        data = json.loads(script_tag.string)
        # open('/t/raw.json', 'w').write(json.dumps(data, indent=2, sort_keys=True))
        cover_url = safe_dict(data, "props", "pageProps", "content", "image", "main", "url", "canonical")
        #print('Got cover:', cover_url)

        feeds_dict = defaultdict(list)
        for part in safe_dict(data, "props", "pageProps", "content", "hasPart", "parts"):
            section = safe_dict(part, "print", "section", "headline") or ''
            title = safe_dict(part, "headline") or ''
            url = safe_dict(part, "url", "canonical") or ''
            if not section or not title or not url:
                continue
            desc = safe_dict(part, "description") or ''
            sub = safe_dict(part, "subheadline") or ''
            if sub and section != sub:
                desc = sub + ' :: ' + desc
            if '/interactive/' in url:
                #print('Skipping interactive article:', title, url)
                continue
            feeds_dict[section].append({"title": title, "url": url, "description": desc})
            #print(' ', title, url, '\n   ', desc)
        return [(section, articles) for section, articles in feeds_dict.items()]
    else:
        return []

#ans = parse_index('2023-09-16')

#print(ans)

def E(parent, name, text='', **attrs):
    ans = parent.makeelement(name, **attrs)
    ans.text = text
    parent.append(ans)
    return ans


def process_node(node, html_parent):
    ntype = node.get('type')
    if ntype == 'tag':
        c = html_parent.makeelement(node['name'])
        c.attrib.update({k: v or '' for k, v in node.get('attribs', {}).items()})
        html_parent.append(c)
        for nc in node.get('children', ()):
            process_node(nc, c)
    elif ntype == 'text':
        text = node.get('data')
        if text:
            #text = replace_entities(text)
            if len(html_parent):
                t = html_parent[-1]
                t.tail = (t.tail or '') + text
            else:
                html_parent.text = (html_parent.text or '') + text


def safe_dict(data, *names):
    ans = data
    for x in names:
        ans = ans.get(x) or {}
    return ans

class JSONHasNoContent(ValueError):
    pass

# 从json里将数据加载到root里???

def load_article_from_json(raw, root):
    # open('/t/raw.json', 'w').write(raw)
    try:
        data = json.loads(raw)['props']['pageProps']['content']
    except KeyError as e:
        raise JSONHasNoContent(e)
    if isinstance(data, list):
        data = data[0]
    head = root.xpath('//head')[0]
    for child in tuple(head):
        if child.tag == 'noscript':
            head.remove(child)
    body = root.xpath('//body')[0]
    for child in tuple(body):
        body.remove(child)
    article = E(body, 'article')
    E(article, 'h4', data['subheadline'], style='color: red; margin: 0')
    E(article, 'h1', data['headline'], style='font-size: x-large')
    E(article, 'div', data['description'], style='font-style: italic')
    E(article, 'div', (data['datePublishedString'] or '') + ' | ' + (data['dateline'] or ''), style='color: gray; margin: 1em')
    main_image_url = safe_dict(data, 'image', 'main', 'url').get('canonical')
    if main_image_url:
        div = E(article, 'div')
        try:
            E(div, 'img', src=main_image_url)
        except Exception:
            pass
    for node in data.get('text') or ():
        process_node(node, article)


def cleanup_html_article(root):
    main = root.xpath('//main')[0]
    body = root.xpath('//body')[0]
    for child in tuple(body):
        body.remove(child)
    body.append(main)
    main.set('id', '')
    main.tag = 'article'
    for x in root.xpath('//*[@style]'):
        x.set('style', '')
    for x in root.xpath('//button'):
        x.getparent().remove(x)


def classes(classes):
    q = frozenset(classes.split(' '))
    return dict(attrs={
        'class': lambda x: x and frozenset(x.split()).intersection(q)})


def new_tag(soup, name, attrs=()):
    impl = getattr(soup, 'new_tag', None)
    if impl is not None:
        return impl(name, attrs=dict(attrs))
    return Tag(soup, name, attrs=attrs or None)


class NoArticles(Exception):
    pass


def preprocess_raw_html(raw):
    # open('/t/raw.html', 'wb').write(raw.encode('utf-8'))
    root = parse(raw)
    script = root.xpath('//script[@id="__NEXT_DATA__"]')
    if script:
        try:
            load_article_from_json(script[0].text, root)
        except JSONHasNoContent:
            cleanup_html_article(root)
    for div in root.xpath('//div[@class="lazy-image"]'):
        noscript = list(div.iter('noscript'))
        if noscript and noscript[0].text:
            img = list(parse(noscript[0].text).iter('img'))
            if img:
                p = noscript[0].getparent()
                idx = p.index(noscript[0])
                p.insert(idx, p.makeelement('img', src=img[0].get('src')))
                p.remove(noscript[0])
    for x in root.xpath('//*[name()="script" or name()="style" or name()="source" or name()="meta"]'):
        x.getparent().remove(x)
    # the economist uses <small> for small caps with a custom font
    for x in root.xpath('//small'):
        if x.text and len(x) == 0:
            x.text = x.text.upper()
            x.tag = 'span'
            x.set('style', 'font-variant: small-caps')
    raw = etree.tostring(root, encoding='unicode')
    return raw

def gen_md(raw,path):
    #print(path)
    root = parse(raw)
    script = root.xpath('//script[@id="__NEXT_DATA__"]')
    if script:
        try:
            data = json.loads(script[0].text)['props']['pageProps']['content']
            with open('next.json', 'wb+') as next:
                next.write(script[0].text.encode('utf-8'))
        except JSONHasNoContent:
            pass
        del data['ad']
        # print(data);
    # 设置文件名
    #mdFile = MdUtils(file_name=data['headline'])
    mdFile = MdUtils(file_name=path+data['headline']+'.md')

    # 下载图片的agent
    opener=urllib.request.build_opener()
    opener.addheaders=[('User-Agent','Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/36.0.1941.0 Safari/537.36')]
    urllib.request.install_opener(opener)

    # 非标设置,手动写入
    mdFile.write('###### '+data['subheadline'])
    mdFile.new_header(level=1, title=data['headline'])
    mdFile.write('##### '+data['description'])

    img_url = None
    if(data['image']['main'] != None):
        img_url = data['image']['main']['url']['canonical']
    elif(data['image']['promo'] != None):
        img_url = data['image']['promo']['url']['canonical']
    
    if(img_url != None):
        # download image
        os.makedirs(path+'/images', exist_ok=True)
        urllib.request.urlretrieve(img_url, path+'/images/'+img_url.split('/')[-1])
        mdFile.write('\n![image](images/'+img_url.split('/')[-1]+')')

    mdFile.write('\n>'+data['datePublishedString'])
    #temp = data['body']
    #print(temp)

    # 正文内容
    body = json.loads(script[0].text)['props']['pageProps']['cp2Content']
    for item in body['body']:
        if(item['type'] == 'CROSSHEAD'):
            mdFile.new_paragraph(item['text'],bold_italics_code='bi', color='purple')
        elif(item['type'] == 'PARAGRAPH'):
            print(item)
            mdFile.new_paragraph(item['textHtml'])
        elif(item['type'] == 'IMAGE'):
            # download image
            os.makedirs(path+'/images', exist_ok=True)
            urllib.request.urlretrieve(item['url'], path+'/images/'+item['url'].split('/')[-1])
            picname = item['url'].split('/')[-1]
            mdFile.write('\n![image](images/'+picname+')')
        elif(item['type'] == 'INFOGRAPHIC'):
            # download image
            print(item['fallback']['url'])
            os.makedirs(path+'images', exist_ok=True)
            urllib.request.urlretrieve(item['fallback']['url'], path+'images/'+item['fallback']['url'].split('/')[-1])
            mdFile.write('\n![image]('+'images/'+item['fallback']['url'].split('/')[-1]+')')
        elif(item['type'] == 'BOOK_INFO'):
            mdFile.new_paragraph(item['textHtml'])
            #https://www.economist.com/the-economist-reads/2023/11/03/six-books-you-didnt-know-were-propaganda
            #https://www.economist.com/culture/2023/11/02/hong-kongs-year-of-protest-now-feels-like-a-mirage
        elif(item['type'] == 'INFOBOX'):
            components = item['components']
            for component in components:
                mdFile.new_paragraph(component['textHtml'])
        elif(item['type'] == 'VIDEO'):
            #https://www.economist.com/europe/2023/10/29/trenches-and-tech-on-ukraines-southern-front
            pass
        else:
            print(item)
            #mdFile.new_line(mdFile.new_reference_image(text='aaa',path=item['url'], reference_tag='im'))
            #print(item)
            #mdFile.new_paragraph(item['textHtml'])
            #mdFile.new_paragraph(item['text'])

    mdFile.create_md_file()


def parse_page(url):
    html_doc = requests.get(url);
    with open('orig.html', 'wb+') as f:
        f.write(html_doc.content)
    #soup = BeautifulSoup(html_doc.content, 'html.parser');
    #print(soup)

    content = preprocess_raw_html(html_doc.content);
    with open('processed-1.html', 'wb+') as f1:
        f1.write(content.encode('utf-8'))
    #print(content)

    gen_md(html_doc.content,'./temp/')

#ans = parse_page('https://www.economist.com/international/2023/11/02/israel-is-more-popular-than-social-media-posts-suggest')
#ans = parse_page('https://www.economist.com/united-states/2023/11/02/can-a-presley-win-mississippi')
#ans = parse_page('https://www.economist.com/europe/2023/11/01/ukraines-commander-in-chief-on-the-breakthrough-he-needs-to-beat-russia')
#ans = parse_page('https://www.economist.com/economic-and-financial-indicators/2023/11/02/economic-data-commodities-and-markets')
#ans = parse_page('https://www.economist.com/the-economist-reads/2023/11/03/six-books-you-didnt-know-were-propaganda')
#ans = parse_page('https://www.economist.com/business/2023/10/30/how-to-get-the-lying-out-of-hiring')
ans = parse_page('https://www.economist.com/culture/2023/11/02/a-show-on-manet-and-degas-examines-creative-rivalry')