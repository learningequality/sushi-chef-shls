#!/usr/bin/env python
from bs4 import BeautifulSoup
import cgi
import copy
import json
import os
import re
import requests
import shutil
import sys
import tempfile
import youtube_dl


from le_utils.constants import licenses, content_kinds, file_types
from ricecooker.chefs import JsonTreeChef
from ricecooker.config import LOGGER
from ricecooker.classes.nodes import ChannelNode, TopicNode, DocumentNode
from ricecooker.classes.files import DocumentFile
from ricecooker.classes.licenses import get_license
from ricecooker.utils.caching import (CacheForeverHeuristic, FileCache, CacheControlAdapter)
from ricecooker.utils.jsontrees import write_tree_to_json_tree

# BOX TOKEN
#################################################################################
# Need to get a new Developer Token  before running chef because expires after one hour
# go to 
BOXAPI_DEVELOPER_TOKEN = open('credentials/box_com_access_token.txt').read().strip()


UNOCONV_SERVICE_URL = os.environ.get('UNOCONV_SERVICE_URL', 'http://35.185.105.222:8989')
if UNOCONV_SERVICE_URL is None:
    print('Need to set environment variable UNOCONV_SERVICE_URL to point to the '
          'unoconv conversion service. Ask in @sushi-chefs channel.')
    sys.exit(1)
if UNOCONV_SERVICE_URL.endswith('/'):
    UNOCONV_SERVICE_URL = UNOCONV_SERVICE_URL.rstrip('/')


# CHANNEL INFO
#################################################################################
SHLS_CHANNEL_NAME = 'Safe Healing and Learning Spaces Toolkit' # or SHLS Toolkit 
SHLS_DOMAIN = 'shls.rescue.org'
SHLS_START_URL = 'http://shls.rescue.org/'
SHLS_CHANNEL_DESCRIPTION = ""
"A Safe Healing and Learning Space (SHLS) is a secure, "
"caring and predictable place where children and adolescents living in conflict "
"and crisis settings can learn, develop and be protected. The SHLS Toolkit "
"provides child protection and education practitioners with all of the content "
"needed to initiate an SHLS program."
SHLS_LICENSE_DICT = get_license(licenses.PUBLIC_DOMAIN,
                                copyright_holder='USAID and International Rescue Committee').as_dict()
TREES_DATA_DIR = 'chefdata/transformed'
CRAWLING_STAGE_OUTPUT =  'chefdata/trees/shls_web_resource_tree.json'
SCRAPING_STAGE_OUTPUT = 'chefdata/trees/shls_downloaded_resources.json'
DOWNLOADED_FILES_DIR = 'chefdata/downloaded'
TRANSFORMED_FILES_DIR = 'chefdata/transformed'
TRANSFORMED_STAGE_OUTPUT = 'chefdata/trees/shls_transformed_resources.json'


# HTTP caching logic
cache = FileCache('.webcache')
basic_adapter = CacheControlAdapter(cache=cache)
forever_adapter = CacheControlAdapter(heuristic=CacheForeverHeuristic(), cache=cache)
SESSION = requests.Session()
SESSION.mount('http://', basic_adapter)
SESSION.mount('https://', basic_adapter)
SESSION.mount('http://' + SHLS_DOMAIN, forever_adapter)
SESSION.mount('https://' + SHLS_DOMAIN, forever_adapter)







# HELPER METHODS
################################################################################

def make_request(url, timeout=60, *args, method='GET', **kwargs):
    """
    Failure-resistant HTTP GET/HEAD request helper method.
    """
    retry_count = 0
    max_retries = 5
    while True:
        try:
            response = SESSION.request(method, url, *args, timeout=timeout, **kwargs)
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            retry_count += 1
            LOGGER.warning("Connection error ('{msg}'); about to perform retry {count} of {trymax}."
                           .format(msg=str(e), count=retry_count, trymax=max_retries))
            time.sleep(retry_count * 1)
            if retry_count >= max_retries:
                LOGGER.error("FAILED TO RETRIEVE:" + str(url))
                return None
    if response.status_code != 200:
        LOGGER.error("ERROR " + str(response.status_code) + ' when getting url=' + url)
        return None
    return response

def download_page(url, *args, **kwargs):
    """
    Download `url` (following redirects) and soupify response contents.
    Returns (final_url, page) where final_url is URL afrer following redirects.
    """
    response = make_request(url, *args, **kwargs)
    if not response:
        return (None, None)
    html = response.text
    page = BeautifulSoup(html, "html.parser")
    LOGGER.debug('Downloaded page ' + str(url))
    return (response.url, page)

def get_text(element):
    """
    Extract text contents of `element`, normalizing newlines to spaces and stripping.
    """
    if element is None:
        return ''
    else:
        return element.get_text().replace('\r', '').replace('\n', ' ').strip()


# BOX.COM DOWNLOAD HELPERS
################################################################################

BOXAPI_SHARED_ITEMS = "https://api.box.com/2.0/shared_items?fields=type,id"
BOXAPI_FILES_CONTENT = "https://api.box.com/2.0/files/{file_id}/content"
BOXAPI_FOLDER_DETAILS = 'https://api.box.com/2.0/folders/{folder_id}'
BOXAPI_FOLDER_ITEMS = 'https://api.box.com/2.0/folders/{folder_id}/items'

def get_shared_item(shared_link):
    headers = {
        "Authorization": "Bearer " + BOXAPI_DEVELOPER_TOKEN,
        "BoxApi": "shared_link=" + shared_link,
    }
    # GET1: get file id for this shared link
    response1 = requests.get(BOXAPI_SHARED_ITEMS, headers=headers)
    json_data = response1.json()
    shared_type, shared_id = json_data['type'], json_data['id']
    if shared_type == 'file':
        folder_id = None
        file_id = shared_id
    elif shared_type == 'folder':
        folder_id = shared_id
        file_id = None
    # print(shared_type, folder_id, file_id)
    return shared_type, folder_id, file_id


def box_download_file(file_id, shared_link, destdir=DOWNLOADED_FILES_DIR):
    headers = {
        "Authorization": "Bearer " + BOXAPI_DEVELOPER_TOKEN,
        "BoxApi": "shared_link=" + shared_link,
    }
    # GET2: get actual file data
    response = requests.get(BOXAPI_FILES_CONTENT.format(file_id=file_id), headers=headers)
    content_disposition = response.headers['Content-Disposition']
    _, params = cgi.parse_header(response.headers['Content-Disposition'])
    filename = params['filename']
    out_path = os.path.join(destdir, filename)
    with open(out_path, 'wb') as outf:
        outf.write(response.content)
        print('Saved file', out_path, 'of size', len(response.content)/1024/1024, 'MB')
    return out_path

def box_download_folder(folder_id, shared_link, destdir=DOWNLOADED_FILES_DIR):
    """
    Return a dict {'title': '',  'children': [ {'path':'local/path/to/file.pdf'}]  }
    """
    headers = {
        "Authorization": "Bearer " + BOXAPI_DEVELOPER_TOKEN,
        "BoxApi": "shared_link=" + shared_link,
    }
    
    # Get deets
    response1 = requests.get(BOXAPI_FOLDER_DETAILS.format(folder_id=folder_id), headers=headers)
    folder_data = response1.json()
    folder_name = folder_data['name']
    folder_dict = dict(
        title=folder_name,
        source_id='box_folder:' + folder_id,
        children=[]
    )
    folder_path = os.path.join(destdir, folder_name)
    if not os.path.exists(folder_path):
        os.mkdir(folder_path)

    # Get contents
    response2 = requests.get(BOXAPI_FOLDER_ITEMS.format(folder_id=folder_id), headers=headers)
    json_data = response2.json()
    for entry in json_data['entries']:
        if entry['type'] == 'file':
            filename = entry['name']
            file_id = entry['id']
            file_path = box_download_file(file_id, shared_link, destdir=folder_path)
            filename = os.path.basename(file_path)
            file_dict = dict(
                title=filename,
                kind='shls_link',
                path=file_path,
                source_id = 'box_file:' + file_id,
            )
            folder_dict['children'].append(file_dict)
        else:
            print('Skipping entry', entry)
    
    return folder_dict















# CRALING
#################################################################################


def crawl_shls(start_url):
    _, page = download_page(start_url)
    topic_tiles = page.find_all('a', class_='c-tile')
    web_resource_tree = dict(
        title='The SHLS web_resource_tree',
        children=[],
    )

    # Extract brochure
    intro_div = page.find('div', class_='ts-large-intro')
    into_links = intro_div.find_all('a')
    for link in into_links:
        link_href = link['href']
        if 'rescue.box.com' in link_href:
            doc_dict = dict(
                kind='shls_link',
                title='IRC SHLS Toolkit Brochure',
                url=link_href,
            )
            web_resource_tree['children'].append(doc_dict)

    # 6 subject tiles
    for tile in topic_tiles:
        subject_href = tile['href']
        if 'printing-guide' in subject_href:
            continue
        title = get_text(tile.find('header').find('h2'))
        description = tile.find('div', class_='c-tile__content').get_text().strip()
        subject_subtree  = dict(
            kind='shls_subject',
            title=title,
            children = [],
        )
        web_resource_tree['children'].append(subject_subtree)


        print('Downloading subject page', title, subject_href)
        _, subject_page = download_page(subject_href)
        list_items = subject_page.find_all('li', class_='c-document-list__item')
        for list_item in list_items:
            thumbnail_url = list_item.find('aside').find('img')['src']
            main_div = list_item.find('div', class_='o-column')
            section_title = main_div.find('h1').get_text().strip()
            section_description = get_text(main_div.find('div', class_='c-document-list__content'))
            section_dict = dict(
                kind='shls_section',
                title=section_title,
                description=section_description,
                thumbnail=thumbnail_url,
                children=[],
            )
            subject_subtree['children'].append(section_dict)
            print('       ', section_title)
            # print('       ', section_description)
            # Docs for each language
            language_divs = main_div.find_all('div', class_='c-document-list__downloads')
            for language_div in language_divs:
                language_name = get_text(language_div.find('h4', class_='ts-heading-4'))
                language_dict = dict(
                    kind='shls_language',
                    title=language_name,
                    children=[],
                )
                section_dict['children'].append(language_dict)
                print('         ', language_name)
                box_links = language_div.find_all('a', class_='c-button')
                for box_link in box_links:
                    doc_url = box_link['href']
                    # delete non-title spans
                    unwanted_spans = box_link.find_all('span')
                    for unwanted_span in unwanted_spans:
                        unwanted_span.extract()
                    doc_title = get_text(box_link)
                    doc_dict = dict(
                        kind='shls_link',
                        title=doc_title,
                        language_name=language_name,
                        url=doc_url,
                    )
                    language_dict['children'].append(doc_dict)
                    print('             doc=', doc_title)
            
            # Extra stuff
            extra_heading = main_div.find('h4', recursive=False)
            if extra_heading:
                extra_heading_title = get_text(extra_heading)
                extras_dict = dict(
                    kind='shls_extras',
                    title=extra_heading_title,
                    children=[],
                )
                section_dict['children'].append(extras_dict)
                print('            ', extra_heading_title)
                extra_items = extra_heading.findNext('ul').find_all('li')
                for extra_item in extra_items:
                    extra_link = extra_item.find('a')
                    unwanted_spans = extra_link.find_all('span')
                    for unwanted_span in unwanted_spans:
                        unwanted_span.extract()
                    extra_title = get_text(extra_link)
                    doc_dict = dict(
                        kind='shls_link',
                        title=extra_title,
                        url=extra_link['href'],
                    )
                    extras_dict['children'].append(doc_dict)
                    print('                  extra=', extra_title)

    with open(CRAWLING_STAGE_OUTPUT, 'w') as outf:
        json.dump(web_resource_tree, outf, indent=2)
    return web_resource_tree







# VIMEO
################################################################################

def get_vimeo_info(url):
    info = None
    ydl_options = {
        'outtmpl': '%(id)s.%(ext)s',  # use the video id as filename
        'writethumbnail': False,
        'no_warnings': True,
        'continuedl': False,
        'restrictfilenames': True,
        'quiet': False,
        'format': "bestvideo[height<={maxheight}][ext=mp4]+bestaudio[ext=m4a]/best[height<={maxheight}][ext=mp4]".format(maxheight='720'),
    }
    with youtube_dl.YoutubeDL(ydl_options) as ydl:
        try:
            ydl.add_default_info_extractors()
            info = ydl.extract_info(url, download=False)
        except (youtube_dl.utils.DownloadError,youtube_dl.utils.ContentTooShortError,youtube_dl.utils.ExtractorError) as e:
            print('error_occured')

    return info



REAL_TITLE_PAT = re.compile(r'This is \"(?P<title>.*)\" by .*')

def downalod_vimeo_playlist(playlist_url, title):
    info = get_vimeo_info(playlist_url)
    playlist_dict = dict(
        kind='vimeo_playlist',
        title=title,
        children=[],
    )
    
    for vid in info['entries']:
        title = vid['title']   # bad title
        description = vid['description']
        m = REAL_TITLE_PAT.search(description)
        if m:
            title = m.groupdict()['title']
        web_url = 'https://vimeo.com/' + vid['id']
        thumbnail = vid['thumbnails'][0]['url']
        video_dict = dict(
            kind='vimeo_video',
            title=title,
            web_url=web_url,
            thumbnail=thumbnail,
        )
        playlist_dict['children'].append(video_dict)
    return playlist_dict


# SCRAPING
################################################################################

def scrape_shls():
    print('scraping')
    with open(CRAWLING_STAGE_OUTPUT, 'r') as inf:
        web_resource_tree = json.load(inf)
    
    downloaded_resources = {}
    
    def scrape_subtree(subtree):

        # recurse down th tree
        oldchildren = subtree['children'] if 'children' in subtree else []
        subtree['children'] = []
        for child in oldchildren:

            child_title = child['title'] 
            child_kind = child['kind']
            print('scraping', child_kind, ' title = ', child_title)
            
            
            # Scrape links
            if child_kind == 'shls_link':
                child_url = child['url'] 
                if  'for print' in child_title:
                    continue
                if  'for web' in child_title:
                    child_title = child_title.replace(' for web', '')
                if 'rescue.box.com' in child_url:
                    shared_link = child_url
                    shared_type, folder_id, file_id = get_shared_item(shared_link)
                    
                    if shared_type == 'file':
                        path = box_download_file(file_id, shared_link, destdir=DOWNLOADED_FILES_DIR)
                        del child['url']
                        child['path'] = path
                        child['kind'] = 'shls_link'
                        child['source_id'] = 'box_file:' + file_id
                        subtree['children'].append(child)

                    elif shared_type == 'folder':
                        child_subtree = box_download_folder(folder_id, shared_link)
                        child_subtree['kind'] = 'shls_shared_folder'
                        subtree['children'].append(child_subtree)

                elif 'vimeo.com' in child_url:
                    if child_title.endswith('_ENGLISH'):
                        lang = 'en'
                    if child_title.endswith('_ARABIC'):
                        lang = 'ar'
                    playlist_subtree = downalod_vimeo_playlist(child_url, child_title)
                    playlist_subtree['language'] = lang
                    subtree['children'].append(playlist_subtree)

                else:
                    print('Skipping', child_title, 'child_url=', child_url)


            else:
                # recurse for all non-leaf nodes
                newchild = scrape_subtree(child)
                subtree['children'].append(newchild)

        return subtree

    downloaded_resources = scrape_subtree(web_resource_tree)

    with open(SCRAPING_STAGE_OUTPUT, 'w') as outf:
        json.dump(downloaded_resources, outf, indent=2)
    return downloaded_resources





# TRANSFORM
################################################################################
    
def save_response_content(response, filename):
    with open(filename, 'wb') as localfile:
        localfile.write(response.content)



def convert_file_to_pdf(path, dest_path):
    """
    Uses unoconv microservice at UNOCONV_SERVICE_URL to convert to `path` to PDF,
    and save the PDF as `dest_path`.
    """

    filename_root, path_ext = os.path.splitext(path)

    if path.startswith('//'):
        path = 'http:' + path

    # Download file in case
    if path.startswith('http'):
        response1 = requests.get(path)
        with tempfile.NamedTemporaryFile(suffix=path_ext) as tmpf:
            save_response_content(response1, tmpf.name)
            path = tmpf.name

    # convert it
    microwave_url = UNOCONV_SERVICE_URL + '/unoconv/pdf'
    files = {'file': open(path, 'rb')}
    response = requests.post(microwave_url, files=files)
    save_response_content(response, dest_path)



def transform_local_files():
    print('transforming downloaded resources')
    with open(SCRAPING_STAGE_OUTPUT, 'r') as inf:
        downloaded_resources = json.load(inf)

    transformed_resources = {}
    
    def transform_subtree(subtree):
        """
        Move files from downloade/ to transformed/ folder, convering file formats
        in the process (.xlxs, .docx, .pptx) --> .pdf
        """
        oldchildren = subtree['children'] if 'children' in subtree else []
        subtree['children'] = []
        for child in oldchildren:
            child_title = child['title'] 
            print('transforming title = ', child_title)
            
            path = child.get('path', None)
            
            if path is not None:
                path_pre_ext, path_ext = os.path.splitext(path)
                if path_ext == '.pdf':
                    print('Copying pdf file', path)
                    dest_path = path.replace(DOWNLOADED_FILES_DIR, TRANSFORMED_FILES_DIR)
                    dest_dir = os.path.dirname(dest_path)
                    if not os.path.exists(dest_dir):
                        os.makedirs(dest_dir, exist_ok=True)
                    shutil.copy(path, dest_path)
                    child['path'] = dest_path
                    subtree['children'].append(child)
                elif path_ext in ['.docx', '.xlsx', '.pptx']:
                    dest_path = path_pre_ext.replace(DOWNLOADED_FILES_DIR, TRANSFORMED_FILES_DIR) + '.pdf'
                    if not os.path.exists(dest_path):
                        print('COnverting', path, 'to PDF')
                        dest_dir = os.path.dirname(dest_path)
                        if not os.path.exists(dest_dir):
                            os.makedirs(dest_dir, exist_ok=True)
                        convert_file_to_pdf(path, dest_path)
                    child['path'] = dest_path
                    subtree['children'].append(child)

                else:
                    print('Skipping file', path)

            else:
                # recurse for all non-leaf nodes
                newchild = transform_subtree(child)
                subtree['children'].append(newchild)

        return subtree

    transformed_resources = transform_subtree(downloaded_resources)
    transformed_resources['kind'] = 'transformed_resources_tree'


    with open(TRANSFORMED_STAGE_OUTPUT, 'w') as outf:
        json.dump(transformed_resources, outf, indent=2)
    return transformed_resources




# LOAD
################################################################################

TOPIC_LIKE_KINDS = ["transformed_resources_tree", "shls_subject", "shls_section",
                    "shls_language", "shls_extras", "vimeo_playlist", "shls_shared_folder"]

def create_ricecooker_json_tree(channel_info):
    print('Createing riecooker json tree')
    with open(TRANSFORMED_STAGE_OUTPUT, 'r') as inf:
        transformed_resources = json.load(inf)

    def ricecookerify_subtree(subtree):
        kind = subtree['kind']
        if kind in TOPIC_LIKE_KINDS:
            topic_node = dict(
                kind=content_kinds.TOPIC,
                source_id=subtree.get('source_id', subtree['title']),
                title=subtree['title'],
                description=subtree.get('description', None),
                thumbnail=subtree.get('thumbnail', None),
                license=SHLS_LICENSE_DICT,
                language='en',                             # TODO(set correctly)
                children=[],
            )
            for child in subtree['children']:
                child_node = ricecookerify_subtree(child)
                topic_node['children'].append(child_node)
            return topic_node

        elif kind == "vimeo_video":
            video_node = dict(
                kind=content_kinds.VIDEO,
                source_id=subtree['web_url'],
                language='en',                             # TODO(set correctly)
                title=subtree['title'],
                description=subtree.get('description', ''),
                thumbnail=subtree['thumbnail'],
                license=SHLS_LICENSE_DICT,
                files=[],
            )
            video_file = dict(
                file_type=file_types.VIDEO,
                web_url=subtree['web_url'],
                language='en',                             # TODO(set correctly)
            )
            video_node['files'].append(video_file)
            return video_node

        elif kind == "shls_link":
            document_node = dict(
                kind=content_kinds.DOCUMENT,
                source_id=subtree['source_id'],
                language='en',                             # TODO(set correctly)
                title=subtree['title'],
                description=subtree.get('description', ''),
                thumbnail=subtree.get('thumbnail', None),
                license=SHLS_LICENSE_DICT,
                files=[],
            )
            document_file = dict(
                file_type=file_types.DOCUMENT,
                path=subtree['path'],
                language='en',                             # TODO(set correctly)
            )
            document_node['files'].append(document_file)
            return document_node

        else:
            print('UNKNOWN kind', kind, subtree)

    ricecooker_json_tree = ricecookerify_subtree(transformed_resources)
    ricecooker_json_tree.update(channel_info)
    return ricecooker_json_tree




# CHEF
################################################################################

class SHLSChef(JsonTreeChef):
    RICECOOKER_JSON_TREE = 'ricecooker_json_tree.json'

    def crawl(self, args, options):
        print('crawling')
        crawl_shls(SHLS_START_URL)
        
    def scrape(self, args, options):
        scrape_shls()

    def transform(self, args, options):
        transform_local_files()

    def write_json_tree(self, args, options):
        channel_info = {
            'title': SHLS_CHANNEL_NAME,
            'source_domain': SHLS_DOMAIN,
            'source_id': 'toolkit',
            'language': 'en',   # TODO: change to `mul`
            'thumbnail': 'chefdata/channel_thumbnail.png',
            'description': SHLS_CHANNEL_DESCRIPTION,
        }
        ricecooker_json_tree = create_ricecooker_json_tree(channel_info)
        json_tree_path = self.get_json_tree_path()
        write_tree_to_json_tree(json_tree_path, ricecooker_json_tree)


    def pre_run(self, args, options):
        data_dirs = [TREES_DATA_DIR, DOWNLOADED_FILES_DIR, TRANSFORMED_FILES_DIR]
        for dir in data_dirs:
            if not os.path.exists(dir):
                os.makedirs(dir, exist_ok=True)
        #self.crawl(args, options)
        #self.scrape(args, options)
        #self.transform(args, options)
        #self.write_json_tree(args, options)




# CLI
################################################################################
if __name__ == '__main__':
    """
    Run this script on the command line using:
        python simple_chef.py -v --reset --token=YOURTOKENHERE9139139f3a23232
    """
    simple_chef = SHLSChef()
    simple_chef.main()
