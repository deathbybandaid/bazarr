# coding=utf-8

import os
import re
import logging
from indexer.tmdb_caching_proxy import tmdb
from database import TableShowsRootfolder, TableShows
from indexer.tmdb_caching_proxy import tmdb_func_cache

WordDelimiterRegex = re.compile(r"(\s|\.|,|_|-|=|\|)+")
PunctuationRegex = re.compile(r"[^\w\s]")
CommonWordRegex = re.compile(r"\b(a|an|the|and|or|of)\b\s?")
DuplicateSpacesRegex = re.compile(r"\s{2,}")


def list_series_directories(root_dir):
    series_directories = []

    try:
        root_dir_path = TableShowsRootfolder.select(TableShowsRootfolder.path)\
            .where(TableShowsRootfolder.rootId == root_dir)\
            .dicts()\
            .get()
    except:
        pass
    else:
        if not root_dir_path:
            logging.debug(f'BAZARR cannot find the specified series root folder: {root_dir}')
            return series_directories
        for i, directory_temp in enumerate(os.listdir(root_dir_path['path'])):
            directory_original = re.sub(r"\(\b(19|20)\d{2}\b\)", '', directory_temp).rstrip()
            directory = re.sub(r"\s\b(19|20)\d{2}\b", '', directory_original).rstrip()
            if directory.endswith(', The'):
                directory = 'The ' + directory.rstrip(', The')
            elif directory.endswith(', A'):
                directory = 'A ' + directory.rstrip(', A')
            if not directory.startswith('.'):
                series_directories.append(
                    {
                        'id': i,
                        'directory': directory_temp,
                        'rootDir': root_dir
                    }
                )
    finally:
        return series_directories


def get_series_match(directory):
    directory_temp = directory
    directory_original = re.sub(r"\(\b(19|20)\d{2}\b\)", '', directory_temp).rstrip()
    directory = re.sub(r"\s\b(19|20)\d{2}\b", '', directory_original).rstrip()

    try:
        series_temp = tmdb_func_cache(tmdb.Search().tv, query=directory)
    except Exception as e:
        logging.exception('BAZARR is facing issues indexing series: {0}'.format(repr(e)))
    else:
        matching_series = []
        if series_temp['total_results']:
            for item in series_temp['results']:
                year = None
                if 'first_air_date' in item:
                    year = item['first_air_date'][:4]
                matching_series.append(
                    {
                        'title': item['original_name'],
                        'year': year or 'n/a',
                        'tmdbId': item['id']
                    }
                )
        return matching_series


def get_series_metadata(tmdbid, root_dir_id, dir_name):
    series_metadata = {}
    root_dir_path = TableShowsRootfolder.select(TableShowsRootfolder.path)\
        .where(TableShowsRootfolder.rootId == root_dir_id)\
        .dicts()\
        .get()
    if tmdbid:
        try:
            series_info = tmdb_func_cache(tmdb.TV(tmdbid).info)
            alternative_titles = tmdb_func_cache(tmdb.TV(tmdbid).alternative_titles)
            external_ids = tmdb_func_cache(tmdb.TV(tmdbid).external_ids)
        except Exception as e:
            logging.exception('BAZARR is facing issues indexing series: {0}'.format(repr(e)))
        else:
            images_url = 'https://image.tmdb.org/t/p/w500{0}'

            series_metadata = {
                'rootdir': root_dir_id,
                'title': series_info['original_name'],
                'path': os.path.join(root_dir_path['path'], dir_name),
                'sortTitle': normalize_title(series_info['original_name']),
                'year': series_info['first_air_date'][:4] if series_info['first_air_date'] else None,
                'tmdbId': tmdbid,
                'overview': series_info['overview'],
                'poster': images_url.format(series_info['poster_path']) if series_info['poster_path'] else None,
                'fanart': images_url.format(series_info['backdrop_path'])if series_info['backdrop_path'] else None,
                'alternateTitles': [x['title'] for x in alternative_titles['results']],
                'imdbId': external_ids['imdb_id']
            }

        return series_metadata


def normalize_title(title):
    title = title.lower()

    title = re.sub(WordDelimiterRegex, " ", title)
    title = re.sub(PunctuationRegex, "", title)
    title = re.sub(CommonWordRegex, "", title)
    title = re.sub(DuplicateSpacesRegex, " ", title)

    return title.strip()


def index_all_series():
    TableShows.delete().execute()
    root_dir_ids = TableShowsRootfolder.select(TableShowsRootfolder.rootId, TableShowsRootfolder.path).dicts()
    for root_dir_id in root_dir_ids:
        root_dir_subdirectories = list_series_directories(root_dir_id['rootId'])
        for root_dir_subdirectory in root_dir_subdirectories:
            root_dir_match = get_series_match(root_dir_subdirectory['directory'])
            if root_dir_match:
                directory_metadata = get_series_metadata(root_dir_match[0]['tmdbId'], root_dir_id['rootId'],
                                                         root_dir_subdirectory['directory'])
                if directory_metadata:
                    try:
                        TableShows.insert(directory_metadata).execute()
                    except Exception as e:
                        logging.error(f'BAZARR is unable to insert this series to the database: '
                                      f'"{directory_metadata["path"]}". The exception encountered is "{e}".')