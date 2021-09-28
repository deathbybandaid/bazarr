# coding=utf-8

import datetime
import glob
import hashlib
import json
import logging
import os
import platform
import stat
import time

import pysubs2
import requests
from config import settings
from custom_lang import CustomLanguage
from database import (TableBlacklist, TableBlacklistMovie, TableHistory,
                      TableHistoryMovie)
from deep_translator import GoogleTranslator
from dogpile.cache import make_region
from event_handler import event_stream
from get_args import args
from get_languages import alpha3_from_alpha2, language_from_alpha2
from list_subtitles import store_subtitles, store_subtitles_movie
from subliminal import region as subliminal_cache_region
from subliminal_patch.core import get_subtitle_path
from subliminal_patch.subtitle import Subtitle
from subzero.language import Language
from whichcraft import which

region = make_region().configure('dogpile.cache.memory')


class BinaryNotFound(Exception):
    pass


def history_log(action, series_id, episode_id, description, video_path=None, language=None, provider=None,
                score=None, subs_id=None, subtitles_path=None):
    TableHistory.insert({
        TableHistory.action: action,
        TableHistory.seriesId: series_id,
        TableHistory.episodeId: episode_id,
        TableHistory.timestamp: time.time(),
        TableHistory.description: description,
        TableHistory.video_path: video_path,
        TableHistory.language: language,
        TableHistory.provider: provider,
        TableHistory.score: score,
        TableHistory.subs_id: subs_id,
        TableHistory.subtitles_path: subtitles_path
    }).execute()
    event_stream(type='episode-history')


def blacklist_log(series_id, episode_id, provider, subs_id, language):
    TableBlacklist.insert({
        TableBlacklist.series_id: series_id,
        TableBlacklist.episode_id: episode_id,
        TableBlacklist.timestamp: time.time(),
        TableBlacklist.provider: provider,
        TableBlacklist.subs_id: subs_id,
        TableBlacklist.language: language
    }).execute()
    event_stream(type='episode-blacklist')


def blacklist_delete(provider, subs_id):
    TableBlacklist.delete().where((TableBlacklist.provider == provider) and
                                  (TableBlacklist.subs_id == subs_id))\
        .execute()
    event_stream(type='episode-blacklist', action='delete')


def blacklist_delete_all():
    TableBlacklist.delete().execute()
    event_stream(type='episode-blacklist', action='delete')


def history_log_movie(action, movie_id, description, video_path=None, language=None, provider=None, score=None,
                      subs_id=None, subtitles_path=None):
    TableHistoryMovie.insert({
        TableHistoryMovie.action: action,
        TableHistoryMovie.movieId: movie_id,
        TableHistoryMovie.timestamp: time.time(),
        TableHistoryMovie.description: description,
        TableHistoryMovie.video_path: video_path,
        TableHistoryMovie.language: language,
        TableHistoryMovie.provider: provider,
        TableHistoryMovie.score: score,
        TableHistoryMovie.subs_id: subs_id,
        TableHistoryMovie.subtitles_path: subtitles_path
    }).execute()
    event_stream(type='movie-history')


def blacklist_log_movie(movie_id, provider, subs_id, language):
    TableBlacklistMovie.insert({
        TableBlacklistMovie.movie_id: movie_id,
        TableBlacklistMovie.timestamp: time.time(),
        TableBlacklistMovie.provider: provider,
        TableBlacklistMovie.subs_id: subs_id,
        TableBlacklistMovie.language: language
    }).execute()
    event_stream(type='movie-blacklist')


def blacklist_delete_movie(provider, subs_id):
    TableBlacklistMovie.delete().where((TableBlacklistMovie.provider == provider) and
                                       (TableBlacklistMovie.subs_id == subs_id))\
        .execute()
    event_stream(type='movie-blacklist', action='delete')


def blacklist_delete_all_movie():
    TableBlacklistMovie.delete().execute()
    event_stream(type='movie-blacklist', action='delete')


@region.cache_on_arguments()
def md5(fname):
    hash_md5 = hashlib.md5()
    with open(fname, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


@region.cache_on_arguments()
def get_binaries_from_json():
    try:
        binaries_json_file = os.path.realpath(os.path.join(os.path.dirname(__file__), 'binaries.json'))
        with open(binaries_json_file) as json_file:
            binaries_json = json.load(json_file)
    except OSError:
        logging.exception('BAZARR cannot access binaries.json')
        return []
    else:
        return binaries_json


def get_binary(name):
    installed_exe = which(name)

    if installed_exe and os.path.isfile(installed_exe):
        logging.debug('BAZARR returning this binary: {}'.format(installed_exe))
        return installed_exe
    else:
        logging.debug('BAZARR binary not found in path, searching for it...')
        binaries_dir = os.path.realpath(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'bin'))
        system = platform.system()
        machine = platform.machine()
        dir_name = name

        # deals with exceptions
        if platform.system() == "Windows":  # Windows
            machine = "i386"
            name = "%s.exe" % name
        elif platform.system() == "Darwin":  # MacOSX
            system = 'MacOSX'
        if name in ['ffprobe', 'ffprobe.exe']:
            dir_name = 'ffmpeg'

        exe_dir = os.path.abspath(os.path.join(binaries_dir, system, machine, dir_name))
        exe = os.path.abspath(os.path.join(exe_dir, name))

        binaries_json = get_binaries_from_json()
        binary = next((item for item in binaries_json if item['system'] == system and item['machine'] == machine and
                       item['directory'] == dir_name and item['name'] == name), None)
        if not binary:
            logging.debug('BAZARR binary not found in binaries.json')
            raise BinaryNotFound
        else:
            logging.debug('BAZARR found this in binaries.json: {}'.format(binary))

        if os.path.isfile(exe) and md5(exe) == binary['checksum']:
            logging.debug('BAZARR returning this existing and up-to-date binary: {}'.format(exe))
            return exe
        else:
            try:
                logging.debug('BAZARR creating directory tree for {}'.format(exe_dir))
                os.makedirs(exe_dir, exist_ok=True)
                logging.debug('BAZARR downloading {0} from {1}'.format(name, binary['url']))
                r = requests.get(binary['url'])
                logging.debug('BAZARR saving {0} to {1}'.format(name, exe_dir))
                with open(exe, 'wb') as f:
                    f.write(r.content)
                if system != 'Windows':
                    logging.debug('BAZARR adding execute permission on {}'.format(exe))
                    st = os.stat(exe)
                    os.chmod(exe, st.st_mode | stat.S_IEXEC)
            except Exception:
                logging.exception('BAZARR unable to download {0} to {1}'.format(name, exe_dir))
                raise BinaryNotFound
            else:
                logging.debug('BAZARR returning this new binary: {}'.format(exe))
                return exe


def get_blacklist(media_type):
    if media_type == 'series':
        blacklist_db = TableBlacklist.select(TableBlacklist.provider, TableBlacklist.subs_id).dicts()
    else:
        blacklist_db = TableBlacklistMovie.select(TableBlacklistMovie.provider, TableBlacklistMovie.subs_id).dicts()

    blacklist_list = []
    for item in blacklist_db:
        blacklist_list.append((item['provider'], item['subs_id']))

    return blacklist_list


def cache_maintenance():
    main_cache_validity = 14  # days
    pack_cache_validity = 4  # days

    logging.info("BAZARR Running cache maintenance")
    now = datetime.datetime.now()

    def remove_expired(path, expiry):
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        if mtime + datetime.timedelta(days=expiry) < now:
            try:
                os.remove(path)
            except (IOError, OSError):
                logging.debug("Couldn't remove cache file: %s", os.path.basename(path))

    # main cache
    for fn in subliminal_cache_region.backend.all_filenames:
        remove_expired(fn, main_cache_validity)

    # archive cache
    for fn in glob.iglob(os.path.join(args.config_dir, "*.archive")):
        remove_expired(fn, pack_cache_validity)


def delete_subtitles(media_type, language, forced, hi, media_path, subtitles_path, series_id=None,
                     episode_id=None, movie_id=None):
    if not subtitles_path.endswith('.srt'):
        logging.error('BAZARR can only delete .srt files.')
        return False

    language_log = language
    language_string = language_from_alpha2(language)
    if hi in [True, 'true', 'True']:
        language_log += ':hi'
        language_string += ' HI'
    elif forced in [True, 'true', 'True']:
        language_log += ':forced'
        language_string += ' forced'

    result = language_string + " subtitles deleted from disk."

    if media_type == 'series':
        try:
            os.remove(subtitles_path)
        except OSError:
            logging.exception('BAZARR cannot delete subtitles file: ' + subtitles_path)
            store_subtitles(media_path)
            return False
        else:
            history_log(0, series_id, episode_id, result, language=language_log,
                        video_path=media_path, subtitles_path=subtitles_path)
            store_subtitles(media_path)
            event_stream(type='episode-wanted', action='update', payload=episode_id)
            return True
    else:
        try:
            os.remove(subtitles_path)
        except OSError:
            logging.exception('BAZARR cannot delete subtitles file: ' + subtitles_path)
            store_subtitles_movie(media_path)
            return False
        else:
            history_log_movie(0, movie_id, result, language=language_log, video_path=media_path,
                              subtitles_path=subtitles_path)
            store_subtitles_movie(media_path)
            event_stream(type='movie-wanted', action='update', payload=movie_id)
            return True


def subtitles_apply_mods(language, subtitle_path, mods):
    language = alpha3_from_alpha2(language)
    custom = CustomLanguage.from_value(language, "alpha3")
    if custom is None:
        lang_obj = Language(language)
    else:
        lang_obj = custom.subzero_language()

    sub = Subtitle(lang_obj, mods=mods)
    with open(subtitle_path, 'rb') as f:
        sub.content = f.read()

    if not sub.is_valid():
        logging.exception('BAZARR Invalid subtitle file: ' + subtitle_path)
        return

    content = sub.get_modified_content()
    if content:
        if os.path.exists(subtitle_path):
            os.remove(subtitle_path)

        with open(subtitle_path, 'wb') as f:
            f.write(content)


def translate_subtitles_file(video_path, source_srt_file, to_lang, forced, hi):
    language_code_convert_dict = {
        'he': 'iw',
    }

    to_lang = alpha3_from_alpha2(to_lang)
    lang_obj = Language(to_lang)
    if forced:
        lang_obj = Language.rebuild(lang_obj, forced=True)
    if hi:
        lang_obj = Language.rebuild(lang_obj, hi=True)

    logging.debug('BAZARR is translating in {0} this subtitles {1}'.format(lang_obj, source_srt_file))

    max_characters = 5000

    dest_srt_file = get_subtitle_path(video_path, language=lang_obj, extension='.srt', forced_tag=forced, hi_tag=hi)

    subs = pysubs2.load(source_srt_file, encoding='utf-8')
    lines_list = [x.plaintext for x in subs]
    joined_lines_str = '\n\n\n'.join(lines_list)

    logging.debug('BAZARR splitting subtitles into {} characters blocks'.format(max_characters))
    lines_block_list = []
    translated_lines_list = []
    while len(joined_lines_str):
        partial_lines_str = joined_lines_str[:max_characters]

        if len(joined_lines_str) > max_characters:
            new_partial_lines_str = partial_lines_str.rsplit('\n\n\n', 1)[0]
        else:
            new_partial_lines_str = partial_lines_str

        lines_block_list.append(new_partial_lines_str)
        joined_lines_str = joined_lines_str.replace(new_partial_lines_str, '')

    logging.debug('BAZARR is sending {} blocks to Google Translate'.format(len(lines_block_list)))
    for block_str in lines_block_list:
        try:
            translated_partial_srt_text = GoogleTranslator(source='auto',
                                                           target=language_code_convert_dict.get(lang_obj.basename,
                                                                                                 lang_obj.basename)
                                                           ).translate(text=block_str)
        except Exception:
            return False
        else:
            translated_partial_srt_list = translated_partial_srt_text.split('\n\n\n')
            translated_lines_list += translated_partial_srt_list

    logging.debug('BAZARR saving translated subtitles to {}'.format(dest_srt_file))
    for i, line in enumerate(subs):
        line.plaintext = translated_lines_list[i]
    subs.save(dest_srt_file)

    return dest_srt_file


def check_credentials(user, pw):
    username = settings.auth.username
    password = settings.auth.password
    return hashlib.md5(pw.encode('utf-8')).hexdigest() == password and user == username


def check_health():
    if settings.general.getboolean('use_series'):
        pass
    if settings.general.getboolean('use_movies'):
        pass


def get_health_issues():
    # this function must return a list of dictionaries consisting of to keys: object and issue
    health_issues = []

    return health_issues
