


from bs4 import BeautifulSoup
import requests
import json
from collections import defaultdict




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

ans = parse_index('2023-11-04')

print(ans)