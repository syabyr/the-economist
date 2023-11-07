import os
import sys
import requests
import json
import urllib.request
from bs4 import BeautifulSoup
from lxml import etree
from collections import defaultdict
from html5_parser import parse
from mdutils.mdutils import MdUtils

#date = '2023-11-04'
date = sys.argv[1]

def safe_dict(data, *names):
    ans = data
    for x in names:
        ans = ans.get(x) or {}
    return ans

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


def gen_md(raw,path):
    #print(path)
    root = parse(raw)
    script = root.xpath('//script[@id="__NEXT_DATA__"]')
    if script:
        try:
            data = json.loads(script[0].text)['props']['pageProps']['content']
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
            # 根据个人喜好,选择是否使用html格式
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



# start

os.makedirs(date, exist_ok=True)

ans = parse_index(date)

index = 0
for tile in ans:
    index = index + 1

    print('process:'+str(index)+'.'+tile[0])
    #print(tile[0])
    os.makedirs(date+'/'+str(index)+'.'+tile[0], exist_ok=True)
    #print(tile[1])
    for article in tile[1]:
        print(article['title'])
        print(article['description'])
        print(article['url'])
        html_doc = requests.get(article['url'])
        gen_md(html_doc.content, date+'/'+str(index)+'.'+tile[0]+'/')


#print(ans)