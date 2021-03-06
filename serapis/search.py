#!/usr/bin/env python
# coding=utf-8
"""
Search module
"""
from __future__ import unicode_literals
from __future__ import absolute_import

__author__ = "Manuel Ebert"
__copyright__ = "Copyright 2015, summer.ai"
__date__ = "2015-11-20"
__email__ = "manuel@summer.ai"

import requests
from dateutil.parser import parse as parse_date
from .config import config  # Make sure to use absolute imports here
from serapis.language import is_english
from serapis.extract import PageRequest
from serapis.util import AsynchronousRequest as async
from serapis.util import merge_dict
import time
import logging
import urlparse
log = logging.getLogger('serapis.search')


def search_all(term):
    """
    Performs an asynchronous search operation on various services.

    Args:
        term: str -- term to search for
    Returns:
        list -- List of url objects containing url, doc, author, and other keys.
    """
    log.info("Sarching for '{}'".format(term))

    search_engine = {
        'bing': search_bing,
        'google': search_google
    }.get(config.search_engine)

    if not search_engine:
        log.error("Invalid search engine '{}' defined in config".format(config.search_engine))
        return []

    ddg = async(search_duckduckgo, term)

    search = async(search_and_parse, search_engine, term)

    while not (ddg.done and search.done):
        time.sleep(.5)
    combined = (ddg.value or []) + (search.value or [])
    result = [url_object for url_object in combined if url_object]
    log.info("Searching URLs for '{}' yielded {} results".format(term, len(result)))
    return result


def search_and_parse(search_func, term):
    """
    Wrapper for asynchronous search and parsing. Performs a search and calls
    serapis.extract for each result in parallel. Returns after all results are
    parsed and extracted.

    Args:
        search_func: function -- Search function, e.g. search_google
        term: str -- Term to search for
    Returns:
         list -- List of url objects containing url, doc, author, and other keys.
    """
    search_result = search_func(term)
    jobs = [async(extract_wrapper, url_object, term) for url_object in search_result]
    time_waited = 0
    while not all(jobs) and time_waited < config.max_search_duration:
        time.sleep(.5)
        time_waited += .5

    result = [job.value for job in jobs if job.done]
    log.info("Parsing URLs for '{}' yielded {} results, {} urls lost".format(term, len(result), len(search_result) - len(result)))
    return result


def extract_wrapper(url_object, term):
    try:
        result = PageRequest(url_object['url'], term).structured
    except Exception:
        import traceback
        log.error("Failed to get page {} -- {}".format(url_object['url'], traceback.format_exc()))
        return url_object
    return merge_dict(url_object, result)


def search_diffbot_cache(term):
    response = requests.get('http://api.diffbot.com/v3/search', params={
        'token': config.credentials.diffbot,
        'query': requests.utils.quote('"{}"'.format(term)),
        'col': 'GLOBAL-INDEX'
    }).json()
    if not response.get('objects'):
        if response.get('error'):
            print("Response Error '{}' (code: {})".format(response['error'], response['errorCode']))
        else:
            print("NO RESULTS")
    results = []
    for object in response.get('objects', []):
        if object.get('text'):
            pr = PageRequest(object.get('pageUrl'), term, run=False)
            pr.extract_sentences(object.get('text'))
            result = {
                "title": object.get('title'),
                "url": object.get('pageUrl'),
                'search_provider': 'diffbot',
                "author": object.get('author'),
                "date": parse_date(object.get('date', '')).isoformat(),
                "doc": object.get('text'),
                "sentences": pr.sentences,
                "variants": list(pr.variants)
            }
            results.append(result)
    return results


def search_duckduckgo(term):
    result = []
    try:
        req = requests.get('http://api.duckduckgo.com/?q={}&format=json'.format(term)).json()
    except:
        return result
    if req['AbstractSource'] not in config.duckduckgo_sources:
        return result
    if req.get('Abstract'):
        pr = PageRequest(req['AbstractURL'], term, run=False)
        pr.extract_sentences(req['Abstract'])
        result.append({
            'title': req['Heading'],
            'url': req['AbstractURL'],
            'search_provider': 'duckduckgo',
            'author': None,
            'date': None,
            'source': req['AbstractSource'],
            'doc': req['Abstract'],
            "sentences": pr.sentences,
            "variants": list(pr.variants)
        })
    if req.get('Definition'):
        pr = PageRequest(req['DefinitionURL'], term, run=False)
        pr.extract_sentences(req['Definition'])
        result.append({
            'title': req['Heading'],
            'url': req['DefinitionURL'],
            'source': req['DefinitionSource'],
            'search_provider': 'duckduckgo',
            'author': None,
            'date': None,
            'doc': req['Definition'],
            "sentences": pr.sentences,
            "variants": list(pr.variants)
        })
    log.info("Searching DuckDuckGo for '{}' returned {} results".format(term, len(result)))
    return result


def qualify_search_result(url, text, date=None):
    """Heuristically determines if a search result is worth parsing.

    Args:
        url: str
        text: str -- Preview or summary
        date: str -- ISO8601 formatted
    Returns:
        bool -- True if the search result is worth parsing.
    """
    for domain in config.exclude_domains:
        if domain in url:
            return False
    if text and not is_english(text):
        log.info("Excluded non-english result '{}'".format(text))
        return False
    parts = urlparse.urlparse(url)
    if parts.path.endswith(".pdf"):
        return False
    return True


def search_google(term):
    result = []
    try:
        r = requests.get(
            'https://www.googleapis.com/customsearch/v1',
            params={"key": config.credentials.google, "cx": config.google_cse, 'q': term, "lr": "lang_en"}
        )
        data = r.json().get('items', [])
    except:
        log.error("GOOGLE search failed for '{}' (Status: {} / {})".format(term, r.status_code, r.reason))
        return result

    for search_result in data:
        if qualify_search_result(search_result['link'], search_result['snippet']):
            result.append({
                'url': search_result['link'],
                'search_provider': 'google',
                'date': None,
                'summary': search_result['snippet'],
                'title': search_result['title']
            })
    log.info("Searching Google for '{}' returned {} results".format(term, len(result)))
    return result


def search_bing(term):
    """
    Uses the BING Api to search for a term.

    Args:
        term: str
    Returns
        list -- up to 50 url_objects.
    """
    result = []
    # For Bing, Username = Password = Key
    auth = requests.auth.HTTPBasicAuth(config.credentials.bing, config.credentials.bing)
    headers = {'User-Agent': 'Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 5.1; Trident/4.0; FDM; .NET CLR 2.0.50727; InfoPath.2; .NET CLR 1.1.4322)'}
    try:
        # Microsoft is still terrible at standards such as basic decency and wants us
        # to wrap SOME query parameters into single quotes but not others.
        # Adult: Moderate filters graphically explicit content, but not text smut
        r = requests.post(
            'https://api.datamarket.azure.com/Bing/Search/Web',
            params={'Query': "'{}'".format(term), "$format": "JSON", "Market": "'en-US'", "Options": "'DisableLocationDetection'", "Adult": "'Moderate'"},
            auth=auth, headers=headers
        )
        data = r.json().get('d', {}).get('results', [])
    except:
        log.error("BING search failed for '{}' (Status: {} / {})".format(term, r.status_code, r.reason))
        return result
    for search_result in data:
        if qualify_search_result(search_result['Url'], search_result['Description']):
            result.append({
                'url': search_result['Url'],
                'search_provider': 'bing',
                'date': None,
                'summary': search_result['Description'],
                'title': search_result['Title']
            })
    log.info("Searching Bing for '{}' returned {} results (out of {})".format(term, len(result), len(data)))
    return result
