# coding=utf-8

import ast
import gc
import io
import json
import logging
import operator
import os
import platform
import re
import sys
import time
from datetime import datetime, timedelta
from functools import reduce, wraps
from operator import itemgetter

import apprise
import pretty
import requests
from bs4 import BeautifulSoup as bso
from config import base_url, get_settings, save_settings, settings
from database import (TableBlacklist, TableBlacklistMovie, TableEpisodes,
                      TableHistory, TableHistoryMovie, TableLanguagesProfiles,
                      TableMovies, TableMoviesRootfolder,
                      TableSettingsLanguages, TableSettingsNotifier,
                      TableShows, TableShowsRootfolder,
                      get_audio_profile_languages, get_desired_languages,
                      get_exclusion_clause, get_profiles_list,
                      update_profile_id_list)
from dateutil import rrule
from event_handler import event_stream
from filesystem import browse_bazarr_filesystem
from flask import Blueprint, jsonify, request, session
from flask_restful import Api, Resource, abort
from get_args import args
from get_languages import (alpha2_from_alpha3, alpha3_from_alpha2,
                           language_from_alpha2)
from get_providers import (get_providers, get_providers_auth,
                           get_throttled_providers, list_throttled_providers,
                           reset_throttled_providers)
from get_subtitle import (download_subtitle, episode_download_subtitles,
                          manual_download_subtitle, manual_search,
                          manual_upload_subtitle, movies_download_subtitles,
                          series_download_subtitles,
                          wanted_search_missing_subtitles_movies,
                          wanted_search_missing_subtitles_series)
from indexer.movies.local.movies_indexer import (get_movies_match,
                                                 get_movies_metadata,
                                                 list_movies_directories)
from indexer.series.local.series_indexer import (get_series_match,
                                                 get_series_metadata,
                                                 list_series_directories)
from list_subtitles import (list_missing_subtitles,
                            list_missing_subtitles_movies,
                            movies_scan_subtitles, series_scan_subtitles,
                            store_subtitles, store_subtitles_movie)
from logger import empty_log
from notifier import send_notifications, send_notifications_movie
from peewee import fn
from scheduler import scheduler
from subliminal_patch.core import SUBTITLE_EXTENSIONS, guessit
from subsyncer import subsync
from utils import (blacklist_delete, blacklist_delete_all,
                   blacklist_delete_all_movie, blacklist_delete_movie,
                   blacklist_log, blacklist_log_movie, check_credentials,
                   delete_subtitles, get_health_issues, history_log,
                   history_log_movie, subtitles_apply_mods,
                   translate_subtitles_file)

api_bp = Blueprint('api', __name__, url_prefix=base_url.rstrip('/') + '/api')
api = Api(api_bp)

None_Keys = ['null', 'undefined', '', None]

False_Keys = ['False', 'false', '0']


def authenticate(actual_method):
    @wraps(actual_method)
    def wrapper(*args, **kwargs):
        apikey_settings = settings.auth.apikey
        apikey_get = request.args.get('apikey')
        apikey_post = request.form.get('apikey')
        apikey_header = None
        if 'X-API-KEY' in request.headers:
            apikey_header = request.headers['X-API-KEY']

        if apikey_settings in [apikey_get, apikey_post, apikey_header]:
            return actual_method(*args, **kwargs)

        return abort(401)

    return wrapper


def postprocess(item):
    # Remove ffprobe_cache
    if 'ffprobe_cache' in item:
        del (item['ffprobe_cache'])

    # Parse tags
    if 'tags' in item:
        if item['tags'] is None:
            item['tags'] = []
        else:
            item['tags'] = ast.literal_eval(item['tags'])

    if 'monitored' in item:
        if item['monitored'] is None:
            item['monitored'] = False
        else:
            item['monitored'] = item['monitored'] == 'True'

    if 'hearing_impaired' in item and item['hearing_impaired'] is not None:
        if item['hearing_impaired'] is None:
            item['hearing_impaired'] = False
        else:
            item['hearing_impaired'] = item['hearing_impaired'] == 'True'

    if 'language' in item:
        if item['language'] == 'None':
            item['language'] = None
        elif item['language'] is not None:
            splitted_language = item['language'].split(':')
            item['language'] = {"name": language_from_alpha2(splitted_language[0]),
                                "code2": splitted_language[0],
                                "code3": alpha3_from_alpha2(splitted_language[0]),
                                "forced": True if item['language'].endswith(':forced') else False,
                                "hi": True if item['language'].endswith(':hi') else False}


def postprocessSeries(item):
    postprocess(item)
    # Parse audio language
    if 'audio_language' in item and item['audio_language'] is not None:
        item['audio_language'] = get_audio_profile_languages(series_id=item['seriesId'])

    if 'alternateTitles' in item:
        if item['alternateTitles'] is None:
            item['alternativeTitles'] = []
        else:
            item['alternativeTitles'] = ast.literal_eval(item['alternateTitles'])
        del item["alternateTitles"]

    # Parse seriesType
    if 'seriesType' in item and item['seriesType'] is not None:
        item['seriesType'] = item['seriesType'].capitalize()


def postprocessEpisode(item):
    postprocess(item)
    if 'audio_language' in item and item['audio_language'] is not None:
        item['audio_language'] = get_audio_profile_languages(episode_id=item['episodeId'])

    if 'subtitles' in item:
        if item['subtitles'] is None:
            raw_subtitles = []
        else:
            raw_subtitles = ast.literal_eval(item['subtitles'])
        subtitles = []

        for subs in raw_subtitles:
            subtitle = subs[0].split(':')
            sub = {"name": language_from_alpha2(subtitle[0]),
                   "code2": subtitle[0],
                   "code3": alpha3_from_alpha2(subtitle[0]),
                   "path": subs[1],
                   "forced": False,
                   "hi": False}
            if len(subtitle) > 1:
                sub["forced"] = True if subtitle[1] == 'forced' else False
                sub["hi"] = True if subtitle[1] == 'hi' else False

            subtitles.append(sub)

        item.update({"subtitles": subtitles})

    # Parse missing subtitles
    if 'missing_subtitles' in item:
        if item['missing_subtitles'] is None:
            item['missing_subtitles'] = []
        else:
            item['missing_subtitles'] = ast.literal_eval(item['missing_subtitles'])
        for i, subs in enumerate(item['missing_subtitles']):
            subtitle = subs.split(':')
            item['missing_subtitles'][i] = {"name": language_from_alpha2(subtitle[0]),
                                            "code2": subtitle[0],
                                            "code3": alpha3_from_alpha2(subtitle[0]),
                                            "forced": False,
                                            "hi": False}
            if len(subtitle) > 1:
                item['missing_subtitles'][i].update({
                    "forced": True if subtitle[1] == 'forced' else False,
                    "hi": True if subtitle[1] == 'hi' else False
                })


# TODO: Move
def postprocessMovie(item):
    postprocess(item)
    # Parse audio language
    if 'audio_language' in item and item['audio_language'] is not None:
        item['audio_language'] = get_audio_profile_languages(movie_id=item['movieId'])

    # Parse alternate titles
    if 'alternativeTitles' in item:
        if item['alternativeTitles'] is None:
            item['alternativeTitles'] = []
        else:
            item['alternativeTitles'] = ast.literal_eval(item['alternativeTitles'])

    # Parse failed attempts
    if 'failedAttempts' in item:
        if item['failedAttempts']:
            item['failedAttempts'] = ast.literal_eval(item['failedAttempts'])

    # Parse subtitles
    if 'subtitles' in item:
        if item['subtitles'] is None:
            item['subtitles'] = []
        else:
            item['subtitles'] = ast.literal_eval(item['subtitles'])
        for i, subs in enumerate(item['subtitles']):
            language = subs[0].split(':')
            item['subtitles'][i] = {"path": subs[1],
                                    "name": language_from_alpha2(language[0]),
                                    "code2": language[0],
                                    "code3": alpha3_from_alpha2(language[0]),
                                    "forced": False,
                                    "hi": False}
            if len(language) > 1:
                item['subtitles'][i].update({
                    "forced": True if language[1] == 'forced' else False,
                    "hi": True if language[1] == 'hi' else False
                })

        if settings.general.getboolean('embedded_subs_show_desired'):
            desired_lang_list = get_desired_languages(item['profileId'])
            item['subtitles'] = [x for x in item['subtitles'] if x['code2'] in desired_lang_list or x['path']]

        item['subtitles'] = sorted(item['subtitles'], key=itemgetter('name', 'forced'))

    # Parse missing subtitles
    if 'missing_subtitles' in item:
        if item['missing_subtitles'] is None:
            item['missing_subtitles'] = []
        else:
            item['missing_subtitles'] = ast.literal_eval(item['missing_subtitles'])
        for i, subs in enumerate(item['missing_subtitles']):
            language = subs.split(':')
            item['missing_subtitles'][i] = {"name": language_from_alpha2(language[0]),
                                            "code2": language[0],
                                            "code3": alpha3_from_alpha2(language[0]),
                                            "forced": False,
                                            "hi": False}
            if len(language) > 1:
                item['missing_subtitles'][i].update({
                    "forced": True if language[1] == 'forced' else False,
                    "hi": True if language[1] == 'hi' else False
                })


class SystemAccount(Resource):
    def post(self):
        if settings.auth.type != 'form':
            return '', 405

        action = request.args.get('action')
        if action == 'login':
            username = request.form.get('username')
            password = request.form.get('password')
            if check_credentials(username, password):
                session['logged_in'] = True
                return '', 204
        elif action == 'logout':
            session.clear()
            gc.collect()
            return '', 204

        return '', 401


class System(Resource):
    @authenticate
    def post(self):
        from server import webserver
        action = request.args.get('action')
        if action == "shutdown":
            webserver.shutdown()
        elif action == "restart":
            webserver.restart()
        return '', 204


class Badges(Resource):
    @authenticate
    def get(self):
        episodes_conditions = [(TableEpisodes.missing_subtitles is not None),
                               (TableEpisodes.missing_subtitles != '[]')]
        episodes_conditions += get_exclusion_clause('series')
        missing_episodes = TableEpisodes.select(TableShows.tags,
                                                TableShows.seriesType,
                                                TableEpisodes.monitored)\
            .join(TableShows)\
            .where(reduce(operator.and_, episodes_conditions))\
            .count()

        movies_conditions = [(TableMovies.missing_subtitles is not None),
                             (TableMovies.missing_subtitles != '[]')]
        movies_conditions += get_exclusion_clause('movie')
        missing_movies = TableMovies.select(TableMovies.tags,
                                            TableMovies.monitored)\
            .where(reduce(operator.and_, movies_conditions))\
            .count()

        throttled_providers = len(eval(str(get_throttled_providers())))

        health_issues = len(get_health_issues())

        result = {
            "episodes": missing_episodes,
            "movies": missing_movies,
            "providers": throttled_providers,
            "status": health_issues
        }
        return jsonify(result)


class Languages(Resource):
    @authenticate
    def get(self):
        history = request.args.get('history')
        if history and history not in False_Keys:
            languages = list(TableHistory.select(TableHistory.language)
                             .where(TableHistory.language is not None)
                             .dicts())
            languages += list(TableHistoryMovie.select(TableHistoryMovie.language)
                              .where(TableHistoryMovie.language is not None)
                              .dicts())
            languages_list = list(set([lang['language'].split(':')[0] for lang in languages]))
            languages_dicts = []
            for language in languages_list:
                code2 = None
                if len(language) == 2:
                    code2 = language
                elif len(language) == 3:
                    code2 = alpha2_from_alpha3(language)
                else:
                    continue

                if not any(x['code2'] == code2 for x in languages_dicts):
                    try:
                        languages_dicts.append({
                            'code2': code2,
                            'name': language_from_alpha2(code2),
                            # Compatibility: Use false temporarily
                            'enabled': False
                        })
                    except Exception:
                        continue
            return jsonify(sorted(languages_dicts, key=itemgetter('name')))

        result = TableSettingsLanguages.select(TableSettingsLanguages.name,
                                               TableSettingsLanguages.code2,
                                               TableSettingsLanguages.enabled)\
            .order_by(TableSettingsLanguages.name).dicts()
        result = list(result)
        for item in result:
            item['enabled'] = item['enabled'] == 1
        return jsonify(result)


class LanguagesProfiles(Resource):
    @authenticate
    def get(self):
        return jsonify(get_profiles_list())


class Notifications(Resource):
    @authenticate
    def patch(self):
        url = request.form.get("url")

        asset = apprise.AppriseAsset(async_mode=False)

        apobj = apprise.Apprise(asset=asset)

        apobj.add(url)

        apobj.notify(
            title='Bazarr test notification',
            body='Test notification'
        )

        return '', 204


class Searches(Resource):
    @authenticate
    def get(self):
        query = request.args.get('query')
        search_list = []

        if query:
            if settings.general.getboolean('use_series'):
                # Get matching series
                series = TableShows.select(TableShows.title,
                                           TableShows.seriesId,
                                           TableShows.year)\
                    .where(TableShows.title.contains(query))\
                    .order_by(TableShows.title)\
                    .dicts()
                series = list(series)
                search_list += series

            if settings.general.getboolean('use_movies'):
                # Get matching movies
                movies = TableMovies.select(TableMovies.title,
                                            TableMovies.movieId,
                                            TableMovies.year) \
                    .where(TableMovies.title.contains(query)) \
                    .order_by(TableMovies.title) \
                    .dicts()
                movies = list(movies)
                search_list += movies

        return jsonify(search_list)


class SystemSettings(Resource):
    @authenticate
    def get(self):
        data = get_settings()

        notifications = TableSettingsNotifier.select().order_by(TableSettingsNotifier.name).dicts()
        notifications = list(notifications)
        for i, item in enumerate(notifications):
            item["enabled"] = item["enabled"] == 1
            notifications[i] = item

        data['notifications'] = dict()
        data['notifications']['providers'] = notifications

        return jsonify(data)

    @authenticate
    def post(self):
        enabled_languages = request.form.getlist('languages-enabled')
        if len(enabled_languages) != 0:
            TableSettingsLanguages.update({
                TableSettingsLanguages.enabled: 0
            }).execute()
            for code in enabled_languages:
                TableSettingsLanguages.update({
                    TableSettingsLanguages.enabled: 1
                })\
                    .where(TableSettingsLanguages.code2 == code)\
                    .execute()
            event_stream("languages")

        languages_profiles = request.form.get('languages-profiles')
        if languages_profiles:
            existing_ids = TableLanguagesProfiles.select(TableLanguagesProfiles.profileId).dicts()
            existing_ids = list(existing_ids)
            existing = [x['profileId'] for x in existing_ids]
            for item in json.loads(languages_profiles):
                if item['profileId'] in existing:
                    # Update existing profiles
                    TableLanguagesProfiles.update({
                        TableLanguagesProfiles.name: item['name'],
                        TableLanguagesProfiles.cutoff: item['cutoff'] if item['cutoff'] != 'null' else None,
                        TableLanguagesProfiles.items: json.dumps(item['items'])
                    })\
                        .where(TableLanguagesProfiles.profileId == item['profileId'])\
                        .execute()
                    existing.remove(item['profileId'])
                else:
                    # Add new profiles
                    TableLanguagesProfiles.insert({
                        TableLanguagesProfiles.profileId: item['profileId'],
                        TableLanguagesProfiles.name: item['name'],
                        TableLanguagesProfiles.cutoff: item['cutoff'] if item['cutoff'] != 'null' else None,
                        TableLanguagesProfiles.items: json.dumps(item['items'])
                    }).execute()
            for profileId in existing:
                # Unassign this profileId from series and movies
                TableShows.update({
                    TableShows.profileId: None
                }).where(TableShows.profileId == profileId).execute()
                TableMovies.update({
                    TableMovies.profileId: None
                }).where(TableMovies.profileId == profileId).execute()
                # Remove deleted profiles
                TableLanguagesProfiles.delete().where(TableLanguagesProfiles.profileId == profileId).execute()

            update_profile_id_list()
            event_stream("languages")

            if settings.general.getboolean('use_series'):
                scheduler.add_job(list_missing_subtitles, kwargs={'send_event': False})
            if settings.general.getboolean('use_movies'):
                scheduler.add_job(list_missing_subtitles_movies, kwargs={'send_event': False})

        # Update Notification
        notifications = request.form.getlist('notifications-providers')
        for item in notifications:
            item = json.loads(item)
            TableSettingsNotifier.update({
                TableSettingsNotifier.enabled: item['enabled'],
                TableSettingsNotifier.url: item['url']
            }).where(TableSettingsNotifier.name == item['name']).execute()

        save_settings(zip(request.form.keys(), request.form.listvalues()))
        event_stream("settings")
        return '', 204


class SystemTasks(Resource):
    @authenticate
    def get(self):
        taskid = request.args.get('taskid')

        task_list = scheduler.get_task_list()

        if taskid:
            for item in task_list:
                if item['job_id'] == taskid:
                    task_list = [item]
                    continue

        return jsonify(data=task_list)

    @authenticate
    def post(self):
        taskid = request.form.get('taskid')

        scheduler.execute_job_now(taskid)

        return '', 204


class SystemLogs(Resource):
    @authenticate
    def get(self):
        logs = []
        with io.open(os.path.join(args.config_dir, 'log', 'bazarr.log'), encoding='UTF-8') as file:
            raw_lines = file.read()
            lines = raw_lines.split('|\n')
            for line in lines:
                if line == '':
                    continue
                raw_message = line.split('|')
                raw_message_len = len(raw_message)
                if raw_message_len > 3:
                    log = dict()
                    log["timestamp"] = raw_message[0]
                    log["type"] = raw_message[1].rstrip()
                    log["message"] = raw_message[3]
                    if raw_message_len > 4 and raw_message[4] != '\n':
                        log['exception'] = raw_message[4].strip('\'').replace('  ', '\u2003\u2003')
                logs.append(log)

            logs.reverse()
        return jsonify(data=logs)

    @authenticate
    def delete(self):
        empty_log()
        return '', 204


class SystemStatus(Resource):
    @authenticate
    def get(self):
        system_status = {}
        system_status.update({'bazarr_version': os.environ["BAZARR_VERSION"]})
        system_status.update({'operating_system': platform.platform()})
        system_status.update({'python_version': platform.python_version()})
        system_status.update({'bazarr_directory': os.path.dirname(os.path.dirname(__file__))})
        system_status.update({'bazarr_config_directory': args.config_dir})
        return jsonify(data=system_status)


class SystemHealth(Resource):
    @authenticate
    def get(self):
        return jsonify(data=get_health_issues())


class SystemReleases(Resource):
    @authenticate
    def get(self):
        filtered_releases = []
        try:
            with io.open(os.path.join(args.config_dir, 'config', 'releases.txt'), 'r', encoding='UTF-8') as f:
                releases = json.loads(f.read())

            for release in releases:
                if settings.general.branch == 'master' and not release['prerelease']:
                    filtered_releases.append(release)
                elif settings.general.branch != 'master' and any(not x['prerelease'] for x in filtered_releases):
                    continue
                elif settings.general.branch != 'master':
                    filtered_releases.append(release)
            if settings.general.branch == 'master':
                filtered_releases = filtered_releases[:5]

            current_version = os.environ["BAZARR_VERSION"]

            for i, release in enumerate(filtered_releases):
                body = release['body'].replace('- ', '').split('\n')[1:]
                filtered_releases[i] = {"body": body,
                                        "name": release['name'],
                                        "date": release['date'][:10],
                                        "prerelease": release['prerelease'],
                                        "current": release['name'].lstrip('v') == current_version}

        except Exception:
            logging.exception(
                'BAZARR cannot parse releases caching file: ' + os.path.join(args.config_dir, 'config', 'releases.txt'))
        return jsonify(data=filtered_releases)


class Series(Resource):
    @authenticate
    def get(self):
        start = request.args.get('start') or 0
        length = request.args.get('length') or -1
        seriesId = request.args.getlist('seriesid[]')

        count = TableShows.select().count()

        if len(seriesId) != 0:
            result = TableShows.select()\
                .where(TableShows.seriesId.in_(seriesId))\
                .order_by(TableShows.sortTitle).dicts()
        else:
            result = TableShows.select().order_by(TableShows.sortTitle).limit(length).offset(start).dicts()

        result = list(result)

        for item in result:
            postprocessSeries(item)

            # Add missing subtitles episode count
            episodes_missing_conditions = [(TableEpisodes.seriesId == item['seriesId']),
                                           (TableEpisodes.missing_subtitles != '[]')]
            episodes_missing_conditions += get_exclusion_clause('series')

            episodeMissingCount = TableEpisodes.select(TableShows.tags,
                                                       TableEpisodes.monitored,
                                                       TableShows.seriesType)\
                .join(TableShows)\
                .where(reduce(operator.and_, episodes_missing_conditions))\
                .count()
            item.update({"episodeMissingCount": episodeMissingCount})

            # Add episode count
            episodeFileCount = TableEpisodes.select(TableShows.tags,
                                                    TableEpisodes.monitored,
                                                    TableShows.seriesType)\
                .join(TableShows)\
                .where(TableEpisodes.seriesId == item['seriesId'])\
                .count()
            item.update({"episodeFileCount": episodeFileCount})

        return jsonify(data=result, total=count)

    @authenticate
    def post(self):
        seriesIdList = request.form.getlist('seriesid')
        profileIdList = request.form.getlist('profileid')

        for idx in range(len(seriesIdList)):
            seriesId = seriesIdList[idx]
            profileId = profileIdList[idx]

            if profileId in None_Keys:
                profileId = None
            else:
                try:
                    profileId = int(profileId)
                except Exception:
                    return '', 400

            TableShows.update({
                TableShows.profileId: profileId
            })\
                .where(TableShows.seriesId == seriesId)\
                .execute()

            list_missing_subtitles(no=seriesId, send_event=False)

            event_stream(type='series', payload=seriesId)

            episode_id_list = TableEpisodes\
                .select(TableEpisodes.episodeId)\
                .where(TableEpisodes.seriesId == seriesId)\
                .dicts()

            for item in episode_id_list:
                event_stream(type='episode-wanted', payload=item['episodeId'])

        event_stream(type='badges')

        return '', 204

    @authenticate
    def patch(self):
        seriesid = request.form.get('seriesid')
        action = request.form.get('action')
        if action == "refresh":
            series_scan_subtitles(seriesid)
            return '', 204
        elif action == "search-missing":
            series_download_subtitles(seriesid)
            return '', 204
        elif action == "search-wanted":
            wanted_search_missing_subtitles_series()
            return '', 204

        return '', 400


class Episodes(Resource):
    @authenticate
    def get(self):
        seriesId = request.args.getlist('seriesid[]')
        episodeId = request.args.getlist('episodeid[]')

        if len(episodeId) > 0:
            result = TableEpisodes.select().where(TableEpisodes.episodeId.in_(episodeId)).dicts()
        elif len(seriesId) > 0:
            result = TableEpisodes.select()\
                .where(TableEpisodes.seriesId.in_(seriesId))\
                .order_by(TableEpisodes.season.desc(), TableEpisodes.episode.desc())\
                .dicts()
        else:
            return "Series or Episode ID not provided", 400

        result = list(result)
        for item in result:
            postprocessEpisode(item)

        return jsonify(data=result)


# PATCH: Download Subtitles
# POST: Upload Subtitles
# DELETE: Delete Subtitles
class EpisodesSubtitles(Resource):
    @authenticate
    def patch(self):
        seriesId = request.args.get('seriesid')
        episodeId = request.args.get('episodeid')
        episodeInfo = TableEpisodes.select(TableEpisodes.title,
                                           TableEpisodes.path,
                                           TableEpisodes.audio_language)\
            .where(TableEpisodes.episodeId == episodeId)\
            .dicts()\
            .get()

        title = episodeInfo['title']
        episodePath = episodeInfo['path']

        language = request.form.get('language')
        hi = request.form.get('hi').capitalize()
        forced = request.form.get('forced').capitalize()

        providers_list = get_providers()
        providers_auth = get_providers_auth()

        audio_language_list = get_audio_profile_languages(episode_id=episodeId)
        if len(audio_language_list) > 0:
            audio_language = audio_language_list[0]['name']
        else:
            audio_language = None

        try:
            result = download_subtitle(episodePath, language, audio_language, hi, forced, providers_list,
                                       providers_auth, title, 'series')
            if result is not None:
                message = result[0]
                path = result[1]
                forced = result[5]
                if result[8]:
                    language_code = result[2] + ":hi"
                elif forced:
                    language_code = result[2] + ":forced"
                else:
                    language_code = result[2]
                provider = result[3]
                score = result[4]
                subs_id = result[6]
                subs_path = result[7]
                history_log(1, seriesId, episodeId, message, path, language_code, provider, score, subs_id,
                            subs_path)
                send_notifications(seriesId, episodeId, message)
                store_subtitles(episodePath)
            else:
                event_stream(type='episode', payload=episodeId)

        except OSError:
            pass

        return '', 204

    @authenticate
    def post(self):
        seriesId = request.args.get('seriesid')
        episodeId = request.args.get('episodeid')
        episodeInfo = TableEpisodes.select(TableEpisodes.title,
                                           TableEpisodes.path,
                                           TableEpisodes.audio_language)\
            .where(TableEpisodes.episodeId == episodeId)\
            .dicts()\
            .get()

        title = episodeInfo['title']
        episodePath = episodeInfo['path']
        audio_language = episodeInfo['audio_language']

        language = request.form.get('language')
        forced = True if request.form.get('forced') == 'true' else False
        hi = True if request.form.get('hi') == 'true' else False
        subFile = request.files.get('file')

        _, ext = os.path.splitext(subFile.filename)

        if ext not in SUBTITLE_EXTENSIONS:
            raise ValueError('A subtitle of an invalid format was uploaded.')

        try:
            result = manual_upload_subtitle(path=episodePath,
                                            language=language,
                                            forced=forced,
                                            hi=hi,
                                            title=title,
                                            media_type='series',
                                            subtitle=subFile,
                                            audio_language=audio_language)

            if result is not None:
                message = result[0]
                path = result[1]
                subs_path = result[2]
                if hi:
                    language_code = language + ":hi"
                elif forced:
                    language_code = language + ":forced"
                else:
                    language_code = language
                provider = "manual"
                score = 360
                history_log(4, seriesId, episodeId, message, path, language_code, provider, score,
                            subtitles_path=subs_path)
                if not settings.general.getboolean('dont_notify_manual_actions'):
                    send_notifications(seriesId, episodeId, message)
                store_subtitles(episodePath)

        except OSError:
            pass

        return '', 204

    @authenticate
    def delete(self):
        seriesId = request.args.get('seriesid')
        episodeId = request.args.get('episodeid')
        episodeInfo = TableEpisodes.select(TableEpisodes.title,
                                           TableEpisodes.path,
                                           TableEpisodes.audio_language)\
            .where(TableEpisodes.episodeId == episodeId)\
            .dicts()\
            .get()

        episodePath = episodeInfo['path']

        language = request.form.get('language')
        forced = request.form.get('forced')
        hi = request.form.get('hi')
        subtitlesPath = request.form.get('path')

        delete_subtitles(media_type='series',
                         language=language,
                         forced=forced,
                         hi=hi,
                         media_path=episodePath,
                         subtitles_path=subtitlesPath,
                         series_id=seriesId,
                         episode_id=episodeId)

        return '', 204


class SeriesRootfolders(Resource):
    @authenticate
    def get(self):
        # list existing series root folders
        root_folders = TableShowsRootfolder.select().dicts()
        root_folders = list(root_folders)
        return jsonify(data=root_folders)

    @authenticate
    def post(self):
        # add a new series root folder
        path = request.form.get('path')
        result = TableShowsRootfolder.insert({
            TableShowsRootfolder.path: path,
            TableShowsRootfolder.accessible: 1,  # TODO: test it instead of assuming it's accessible
            TableShowsRootfolder.error: ''
        }).execute()
        return jsonify(data=list(TableShowsRootfolder.select().where(TableShowsRootfolder.rootId == result).dicts()))


class SeriesDirectories(Resource):
    @authenticate
    def get(self):
        # list series directories inside a specific root folder
        root_folder_id = request.args.get('id')
        return jsonify(data=list_series_directories(root_dir=root_folder_id))


class SeriesLookup(Resource):
    @authenticate
    def get(self):
        # return possible matches from TMDB for a specific series directory
        dir_name = request.args.get('dir_name')
        matches = get_series_match(directory=dir_name)
        return jsonify(data=matches)


class SeriesAdd(Resource):
    @authenticate
    def post(self):
        # add a new series to database
        tmdbId = request.args.get('tmdbid')
        rootdir_id = request.args.get('rootdir_id')
        directory = request.args.get('directory')
        series_metadata = get_series_metadata(tmdbid=tmdbId, root_dir_id=rootdir_id, dir_name=directory)
        if series_metadata and series_metadata['path']:
            try:
                result = TableShows.insert(series_metadata).execute()
            except Exception:
                pass
            else:
                if result:
                    store_subtitles(series_metadata['path'])


class SeriesModify(Resource):
    @authenticate
    def patch(self):
        # modify an existing series in database
        pass


class Movies(Resource):
    @authenticate
    def get(self):
        start = request.args.get('start') or 0
        length = request.args.get('length') or -1
        movieId = request.args.getlist('movieid[]')

        count = TableMovies.select().count()

        if len(movieId) != 0:
            result = TableMovies.select()\
                .where(TableMovies.movieId.in_(movieId))\
                .order_by(TableMovies.sortTitle)\
                .dicts()
        else:
            result = TableMovies.select().order_by(TableMovies.sortTitle).limit(length).offset(start).dicts()
        result = list(result)
        for item in result:
            postprocessMovie(item)

        return jsonify(data=result, total=count)

    @authenticate
    def post(self):
        movieIdList = request.form.getlist('movieid')
        profileIdList = request.form.getlist('profileid')

        for idx in range(len(movieIdList)):
            movieId = movieIdList[idx]
            profileId = profileIdList[idx]

            if profileId in None_Keys:
                profileId = None
            else:
                try:
                    profileId = int(profileId)
                except Exception:
                    return '', 400

            TableMovies.update({
                TableMovies.profileId: profileId
            })\
                .where(TableMovies.movieId == movieId)\
                .execute()

            list_missing_subtitles_movies(no=movieId, send_event=False)

            event_stream(type='movie', payload=movieId)
            event_stream(type='movie-wanted', payload=movieId)
        event_stream(type='badges')

        return '', 204

    @authenticate
    def patch(self):
        movieid = request.form.get('movieid')
        action = request.form.get('action')
        if action == "refresh":
            movies_scan_subtitles(movieid)
            return '', 204
        elif action == "search-missing":
            movies_download_subtitles(movieid)
            return '', 204
        elif action == "search-wanted":
            wanted_search_missing_subtitles_movies()
            return '', 204

        return '', 400


"""
:param language: Alpha2 language code
"""


class MoviesSubtitles(Resource):
    @authenticate
    def patch(self):
        # Download
        movieId = request.args.get('movieid')

        movieInfo = TableMovies.select(TableMovies.title,
                                       TableMovies.path,
                                       TableMovies.audio_language)\
            .where(TableMovies.movieId == movieId)\
            .dicts()\
            .get()

        moviePath = movieInfo['path']

        title = movieInfo['title']
        audio_language = movieInfo['audio_language']

        language = request.form.get('language')
        hi = request.form.get('hi').capitalize()
        forced = request.form.get('forced').capitalize()

        providers_list = get_providers()
        providers_auth = get_providers_auth()

        audio_language_list = get_audio_profile_languages(movie_id=movieId)
        if len(audio_language_list) > 0:
            audio_language = audio_language_list[0]['name']
        else:
            audio_language = None

        try:
            result = download_subtitle(moviePath, language, audio_language, hi, forced, providers_list,
                                       providers_auth, title, 'movie')
            if result is not None:
                message = result[0]
                path = result[1]
                forced = result[5]
                if result[8]:
                    language_code = result[2] + ":hi"
                elif forced:
                    language_code = result[2] + ":forced"
                else:
                    language_code = result[2]
                provider = result[3]
                score = result[4]
                subs_id = result[6]
                subs_path = result[7]
                history_log_movie(1, movieId, message, path, language_code, provider, score, subs_id, subs_path)
                send_notifications_movie(movieId, message)
                store_subtitles_movie(moviePath)
            else:
                event_stream(type='movie', payload=movieId)
        except OSError:
            pass

        return '', 204

    @authenticate
    def post(self):
        # Upload
        # TODO: Support Multiply Upload
        movieId = request.args.get('movieid')
        movieInfo = TableMovies.select(TableMovies.title,
                                       TableMovies.path,
                                       TableMovies.audio_language) \
            .where(TableMovies.movieId == movieId) \
            .dicts() \
            .get()

        moviePath = movieInfo['path']

        title = movieInfo['title']
        audioLanguage = movieInfo['audio_language']

        language = request.form.get('language')
        forced = True if request.form.get('forced') == 'true' else False
        hi = True if request.form.get('hi') == 'true' else False
        subFile = request.files.get('file')

        _, ext = os.path.splitext(subFile.filename)

        if ext not in SUBTITLE_EXTENSIONS:
            raise ValueError('A subtitle of an invalid format was uploaded.')

        try:
            result = manual_upload_subtitle(path=moviePath,
                                            language=language,
                                            forced=forced,
                                            hi=hi,
                                            title=title,
                                            media_type='movie',
                                            subtitle=subFile,
                                            audio_language=audioLanguage)

            if result is not None:
                message = result[0]
                path = result[1]
                subs_path = result[2]
                if hi:
                    language_code = language + ":hi"
                elif forced:
                    language_code = language + ":forced"
                else:
                    language_code = language
                provider = "manual"
                score = 120
                history_log_movie(4, movieId, message, path, language_code, provider, score, subtitles_path=subs_path)
                if not settings.general.getboolean('dont_notify_manual_actions'):
                    send_notifications_movie(movieId, message)
                store_subtitles_movie(moviePath)
        except OSError:
            pass

        return '', 204

    @authenticate
    def delete(self):
        # Delete
        movieId = request.args.get('movieid')
        movieInfo = TableMovies.select(TableMovies.path) \
            .where(TableMovies.movieId == movieId) \
            .dicts() \
            .get()

        moviePath = movieInfo['path']

        language = request.form.get('language')
        forced = request.form.get('forced')
        hi = request.form.get('hi')
        subtitlesPath = request.form.get('path')

        result = delete_subtitles(media_type='movie',
                                  language=language,
                                  forced=forced,
                                  hi=hi,
                                  media_path=moviePath,
                                  subtitles_path=subtitlesPath,
                                  movie_id=movieId)
        if result:
            return '', 202
        else:
            return '', 204


class MoviesRootfolders(Resource):
    @authenticate
    def get(self):
        # list existing movies root folders
        root_folders = TableMoviesRootfolder.select().dicts()
        root_folders = list(root_folders)
        return jsonify(data=root_folders)

    @authenticate
    def post(self):
        # add a new movies root folder
        path = request.form.get('path')
        result = TableMoviesRootfolder.insert({
            TableMoviesRootfolder.path: path,
            TableMoviesRootfolder.accessible: 1,  # TODO: test it instead of assuming it's accessible
            TableMoviesRootfolder.error: ''
        }).execute()
        return jsonify(data=list(TableMoviesRootfolder.select().where(TableMoviesRootfolder.rootId == result).dicts()))


class MoviesDirectories(Resource):
    @authenticate
    def get(self):
        # list movies directories inside a specific root folder
        root_folder_id = request.args.get('id')
        return jsonify(data=list_movies_directories(root_dir=root_folder_id))


class MoviesLookup(Resource):
    @authenticate
    def get(self):
        # return possible matches from TMDB for a specific movie directory
        dir_name = request.args.get('dir_name')
        matches = get_movies_match(directory=dir_name)
        return jsonify(data=matches)


class MoviesAdd(Resource):
    @authenticate
    def post(self):
        # add a new movie to database
        tmdbId = request.args.get('tmdbid')
        rootdir_id = request.args.get('rootdir_id')
        directory = request.args.get('directory')
        movies_metadata = get_movies_metadata(tmdbid=tmdbId, root_dir_id=rootdir_id, dir_name=directory)
        if movies_metadata and movies_metadata['path']:
            try:
                result = TableMovies.insert(movies_metadata).execute()
            except Exception:
                pass
            else:
                if result:
                    store_subtitles_movie(movies_metadata['path'])


class MoviesModify(Resource):
    @authenticate
    def patch(self):
        # modify an existing movie in database
        pass


class Providers(Resource):
    @authenticate
    def get(self):
        history = request.args.get('history')
        if history and history not in False_Keys:
            providers = list(TableHistory.select(TableHistory.provider)
                             .where(TableHistory.provider is not None and TableHistory.provider != "manual")
                             .dicts())
            providers += list(TableHistoryMovie.select(TableHistoryMovie.provider)
                              .where(TableHistoryMovie.provider is not None and TableHistoryMovie.provider != "manual")
                              .dicts())
            providers_list = list(set([x['provider'] for x in providers]))
            providers_dicts = []
            for provider in providers_list:
                providers_dicts.append({
                    'name': provider,
                    'status': 'History',
                    'retry': '-'
                })
            return jsonify(data=sorted(providers_dicts, key=itemgetter('name')))

        throttled_providers = list_throttled_providers()

        providers = list()
        for provider in throttled_providers:
            providers.append({
                "name": provider[0],
                "status": provider[1] if provider[1] is not None else "Good",
                "retry": provider[2] if provider[2] != "now" else "-"
            })
        return jsonify(data=providers)

    @authenticate
    def post(self):
        action = request.form.get('action')

        if action == 'reset':
            reset_throttled_providers()
            return '', 204

        return '', 400


class ProviderMovies(Resource):
    @authenticate
    def get(self):
        # Manual Search
        movieId = request.args.get('movieid')
        movieInfo = TableMovies.select(TableMovies.title,
                                       TableMovies.path,
                                       TableMovies.profileId) \
            .where(TableMovies.movieId == movieId) \
            .dicts() \
            .get()

        title = movieInfo['title']
        moviePath = movieInfo['path']
        profileId = movieInfo['profileId']

        providers_list = get_providers()
        providers_auth = get_providers_auth()

        data = manual_search(moviePath, profileId, providers_list, providers_auth, title, 'movie')
        if not data:
            data = []
        return jsonify(data=data)

    @authenticate
    def post(self):
        # Manual Download
        movieId = request.args.get('movieid')
        movieInfo = TableMovies.select(TableMovies.title,
                                       TableMovies.path,
                                       TableMovies.audio_language) \
            .where(TableMovies.movieId == movieId) \
            .dicts() \
            .get()

        title = movieInfo['title']
        moviePath = movieInfo['path']
        audio_language = movieInfo['audio_language']

        language = request.form.get('language')
        hi = request.form.get('hi').capitalize()
        forced = request.form.get('forced').capitalize()
        selected_provider = request.form.get('provider')
        subtitle = request.form.get('subtitle')

        providers_auth = get_providers_auth()

        audio_language_list = get_audio_profile_languages(movie_id=movieId)
        if len(audio_language_list) > 0:
            audio_language = audio_language_list[0]['name']
        else:
            audio_language = 'None'

        try:
            result = manual_download_subtitle(moviePath, language, audio_language, hi, forced, subtitle,
                                              selected_provider, providers_auth, title, 'movie')
            if result is not None:
                message = result[0]
                path = result[1]
                forced = result[5]
                if result[8]:
                    language_code = result[2] + ":hi"
                elif forced:
                    language_code = result[2] + ":forced"
                else:
                    language_code = result[2]
                provider = result[3]
                score = result[4]
                subs_id = result[6]
                subs_path = result[7]
                history_log_movie(2, movieId, message, path, language_code, provider, score, subs_id, subs_path)
                if not settings.general.getboolean('dont_notify_manual_actions'):
                    send_notifications_movie(movieId, message)
                store_subtitles_movie(moviePath)
        except OSError:
            pass

        return '', 204


class ProviderEpisodes(Resource):
    @authenticate
    def get(self):
        # Manual Search
        episodeId = request.args.get('episodeid')
        episodeInfo = TableEpisodes.select(TableEpisodes.title,
                                           TableEpisodes.path,
                                           TableShows.profileId) \
            .join(TableShows)\
            .where(TableEpisodes.episodeId == episodeId) \
            .dicts() \
            .get()

        title = episodeInfo['title']
        episodePath = episodeInfo['path']
        profileId = episodeInfo['profileId']

        providers_list = get_providers()
        providers_auth = get_providers_auth()

        data = manual_search(episodePath, profileId, providers_list, providers_auth, title,
                             'series')
        if not data:
            data = []
        return jsonify(data=data)

    @authenticate
    def post(self):
        # Manual Download
        seriesId = request.args.get('seriesid')
        episodeId = request.args.get('episodeid')
        episodeInfo = TableEpisodes.select(TableEpisodes.title,
                                           TableEpisodes.path) \
            .where(TableEpisodes.episodeId == episodeId) \
            .dicts() \
            .get()

        title = episodeInfo['title']
        episodePath = episodeInfo['path']

        language = request.form.get('language')
        hi = request.form.get('hi').capitalize()
        forced = request.form.get('forced').capitalize()
        selected_provider = request.form.get('provider')
        subtitle = request.form.get('subtitle')
        providers_auth = get_providers_auth()

        audio_language_list = get_audio_profile_languages(episode_id=episodeId)
        if len(audio_language_list) > 0:
            audio_language = audio_language_list[0]['name']
        else:
            audio_language = 'None'

        try:
            result = manual_download_subtitle(episodePath, language, audio_language, hi, forced, subtitle,
                                              selected_provider, providers_auth, title, 'series')
            if result is not None:
                message = result[0]
                path = result[1]
                forced = result[5]
                if result[8]:
                    language_code = result[2] + ":hi"
                elif forced:
                    language_code = result[2] + ":forced"
                else:
                    language_code = result[2]
                provider = result[3]
                score = result[4]
                subs_id = result[6]
                subs_path = result[7]
                history_log(2, seriesId, episodeId, message, path, language_code, provider, score, subs_id,
                            subs_path)
                if not settings.general.getboolean('dont_notify_manual_actions'):
                    send_notifications(seriesId, episodeId, message)
                store_subtitles(episodePath)
            return result, 201
        except OSError:
            pass

        return '', 204


class EpisodesHistory(Resource):
    @authenticate
    def get(self):
        start = request.args.get('start') or 0
        length = request.args.get('length') or -1
        episodeid = request.args.get('episodeid')

        upgradable_episodes_not_perfect = []
        if settings.general.getboolean('upgrade_subs'):
            days_to_upgrade_subs = settings.general.days_to_upgrade_subs
            minimum_timestamp = ((datetime.datetime.now() - timedelta(days=int(days_to_upgrade_subs))) -
                                 datetime.datetime(1970, 1, 1)).total_seconds()

            if settings.general.getboolean('upgrade_manual'):
                query_actions = [1, 2, 3, 6]
            else:
                query_actions = [1, 3]

            upgradable_episodes_conditions = [(TableHistory.action.in_(query_actions)),
                                              (TableHistory.timestamp > minimum_timestamp),
                                              (TableHistory.score is not None)]
            upgradable_episodes_conditions += get_exclusion_clause('series')
            upgradable_episodes = TableHistory.select(TableHistory.video_path,
                                                      fn.MAX(TableHistory.timestamp).alias('timestamp'),
                                                      TableHistory.score,
                                                      TableShows.tags,
                                                      TableEpisodes.monitored,
                                                      TableShows.seriesType)\
                .join(TableEpisodes) \
                .join(TableShows) \
                .where(reduce(operator.and_, upgradable_episodes_conditions))\
                .group_by(TableHistory.video_path)\
                .dicts()
            upgradable_episodes = list(upgradable_episodes)
            for upgradable_episode in upgradable_episodes:
                if upgradable_episode['timestamp'] > minimum_timestamp:
                    try:
                        int(upgradable_episode['score'])
                    except ValueError:
                        pass
                    else:
                        if int(upgradable_episode['score']) < 360:
                            upgradable_episodes_not_perfect.append(upgradable_episode)

        query_conditions = [(TableEpisodes.title is not None)]
        if episodeid:
            query_conditions.append((TableEpisodes.episodeId == episodeid))
        query_condition = reduce(operator.and_, query_conditions)
        episode_history = TableHistory.select(TableHistory.id,
                                              TableShows.title.alias('seriesTitle'),
                                              TableEpisodes.monitored,
                                              TableEpisodes.season.concat('x').concat(TableEpisodes.episode).alias('episode_number'),
                                              TableEpisodes.title.alias('episodeTitle'),
                                              TableHistory.timestamp,
                                              TableHistory.subs_id,
                                              TableHistory.description,
                                              TableHistory.seriesId,
                                              TableEpisodes.path,
                                              TableHistory.language,
                                              TableHistory.score,
                                              TableShows.tags,
                                              TableHistory.action,
                                              TableHistory.subtitles_path,
                                              TableHistory.episodeId,
                                              TableHistory.provider,
                                              TableShows.seriesType)\
            .join(TableEpisodes) \
            .join(TableShows) \
            .where(query_condition)\
            .order_by(TableHistory.timestamp.desc())\
            .limit(length)\
            .offset(start)\
            .dicts()
        episode_history = list(episode_history)

        blacklist_db = TableBlacklist.select(TableBlacklist.provider, TableBlacklist.subs_id).dicts()
        blacklist_db = list(blacklist_db)

        for item in episode_history:
            # Mark episode as upgradable or not
            item.update({"upgradable": False})
            if {
                "video_path": str(item["path"]),
                "timestamp": float(item["timestamp"]),
                "score": str(item["score"]),
                "tags": str(item["tags"]),
                "monitored": str(item["monitored"]),
                "seriesType": str(item["seriesType"]),
            } in upgradable_episodes_not_perfect:
                if os.path.isfile(item["subtitles_path"]):
                    item.update({"upgradable": True})

            del item["path"]

            postprocessEpisode(item)

            if item['score']:
                item['score'] = str(round((int(item['score']) * 100 / 360), 2)) + "%"

            # Make timestamp pretty
            if item['timestamp']:
                item["raw_timestamp"] = int(item['timestamp'])
                item["parsed_timestamp"] = datetime.datetime.fromtimestamp(int(item['timestamp'])).strftime('%x %X')
                item['timestamp'] = pretty.date(item["raw_timestamp"])

            # Check if subtitles is blacklisted
            item.update({"blacklisted": False})
            if item['action'] not in [0, 4, 5]:
                for blacklisted_item in blacklist_db:
                    if blacklisted_item['provider'] == item['provider'] and \
                            blacklisted_item['subs_id'] == item['subs_id']:
                        item.update({"blacklisted": True})
                        break

        count = TableHistory.select()\
            .join(TableEpisodes)\
            .where(TableEpisodes.title is not None).count()

        return jsonify(data=episode_history, total=count)


class MoviesHistory(Resource):
    @authenticate
    def get(self):
        start = request.args.get('start') or 0
        length = request.args.get('length') or -1
        movieid = request.args.get('movieid')

        upgradable_movies = []
        upgradable_movies_not_perfect = []
        if settings.general.getboolean('upgrade_subs'):
            days_to_upgrade_subs = settings.general.days_to_upgrade_subs
            minimum_timestamp = ((datetime.datetime.now() - timedelta(days=int(days_to_upgrade_subs))) -
                                 datetime.datetime(1970, 1, 1)).total_seconds()

            if settings.general.getboolean('upgrade_manual'):
                query_actions = [1, 2, 3, 6]
            else:
                query_actions = [1, 3]

            upgradable_movies_conditions = [(TableHistoryMovie.action.in_(query_actions)),
                                            (TableHistoryMovie.timestamp > minimum_timestamp),
                                            (TableHistoryMovie.score is not None)]
            upgradable_movies_conditions += get_exclusion_clause('movie')
            upgradable_movies = TableHistoryMovie.select(TableHistoryMovie.video_path,
                                                         fn.MAX(TableHistoryMovie.timestamp).alias('timestamp'),
                                                         TableHistoryMovie.score,
                                                         TableMovies.tags,
                                                         TableMovies.monitored)\
                .join(TableMovies)\
                .where(reduce(operator.and_, upgradable_movies_conditions))\
                .group_by(TableHistoryMovie.video_path)\
                .dicts()
            upgradable_movies = list(upgradable_movies)

            for upgradable_movie in upgradable_movies:
                if upgradable_movie['timestamp'] > minimum_timestamp:
                    try:
                        int(upgradable_movie['score'])
                    except ValueError:
                        pass
                    else:
                        if int(upgradable_movie['score']) < 120:
                            upgradable_movies_not_perfect.append(upgradable_movie)

        query_conditions = [(TableMovies.title is not None)]
        if movieid:
            query_conditions.append((TableMovies.movieId == movieid))
        query_condition = reduce(operator.and_, query_conditions)

        movie_history = TableHistoryMovie.select(TableHistoryMovie.id,
                                                 TableHistoryMovie.action,
                                                 TableMovies.title,
                                                 TableHistoryMovie.timestamp,
                                                 TableHistoryMovie.description,
                                                 TableHistoryMovie.movieId,
                                                 TableMovies.monitored,
                                                 TableHistoryMovie.video_path.alias('path'),
                                                 TableHistoryMovie.language,
                                                 TableMovies.tags,
                                                 TableHistoryMovie.score,
                                                 TableHistoryMovie.subs_id,
                                                 TableHistoryMovie.provider,
                                                 TableHistoryMovie.subtitles_path)\
            .join(TableMovies)\
            .where(query_condition)\
            .order_by(TableHistoryMovie.timestamp.desc())\
            .limit(length)\
            .offset(start)\
            .dicts()
        movie_history = list(movie_history)

        blacklist_db = TableBlacklistMovie.select(TableBlacklistMovie.provider, TableBlacklistMovie.subs_id).dicts()
        blacklist_db = list(blacklist_db)

        for item in movie_history:
            # Mark movies as upgradable or not
            item.update({"upgradable": False})
            if {
                "video_path": str(item["path"]),
                "timestamp": float(item["timestamp"]),
                "score": str(item["score"]),
                "tags": str(item["tags"]),
                "monitored": str(item["monitored"]),
            } in upgradable_movies_not_perfect:
                if os.path.isfile(item["subtitles_path"]):
                    item.update({"upgradable": True})

            del item["path"]

            postprocessMovie(item)

            if item['score']:
                item['score'] = str(round((int(item['score']) * 100 / 120), 2)) + "%"

            # Make timestamp pretty
            if item['timestamp']:
                item["raw_timestamp"] = int(item['timestamp'])
                item["parsed_timestamp"] = datetime.datetime.fromtimestamp(int(item['timestamp'])).strftime('%x %X')
                item['timestamp'] = pretty.date(item["raw_timestamp"])

            # Check if subtitles is blacklisted
            item.update({"blacklisted": False})
            if item['action'] not in [0, 4, 5]:
                for blacklisted_item in blacklist_db:
                    if blacklisted_item['provider'] == item['provider'] and blacklisted_item['subs_id'] == item['subs_id']:
                        item.update({"blacklisted": True})
                        break

        count = TableHistoryMovie.select()\
            .join(TableMovies)\
            .where(TableMovies.title is not None)\
            .count()

        return jsonify(data=movie_history, total=count)


class HistoryStats(Resource):
    @authenticate
    def get(self):
        timeframe = request.args.get('timeframe') or 'month'
        action = request.args.get('action') or 'All'
        provider = request.args.get('provider') or 'All'
        language = request.args.get('language') or 'All'

        # timeframe must be in ['week', 'month', 'trimester', 'year']
        if timeframe == 'year':
            delay = 364 * 24 * 60 * 60
        elif timeframe == 'trimester':
            delay = 90 * 24 * 60 * 60
        elif timeframe == 'month':
            delay = 30 * 24 * 60 * 60
        elif timeframe == 'week':
            delay = 6 * 24 * 60 * 60

        now = time.time()
        past = now - delay

        history_where_clauses = [(TableHistory.timestamp.between(past, now))]
        history_where_clauses_movie = [(TableHistoryMovie.timestamp.between(past, now))]

        if action != 'All':
            history_where_clauses.append((TableHistory.action == action))
            history_where_clauses_movie.append((TableHistoryMovie.action == action))
        else:
            history_where_clauses.append((TableHistory.action.in_([1, 2, 3])))
            history_where_clauses_movie.append((TableHistoryMovie.action.in_([1, 2, 3])))

        if provider != 'All':
            history_where_clauses.append((TableHistory.provider == provider))
            history_where_clauses_movie.append((TableHistoryMovie.provider == provider))

        if language != 'All':
            history_where_clauses.append((TableHistory.language == language))
            history_where_clauses_movie.append((TableHistoryMovie.language == language))

        history_where_clause = reduce(operator.and_, history_where_clauses)
        history_where_clause_movie = reduce(operator.and_, history_where_clauses_movie)

        data_series = TableHistory.select(fn.strftime('%Y-%m-%d', TableHistory.timestamp, 'unixepoch').alias('date'),
                                          fn.COUNT(TableHistory.id).alias('count'))\
            .where(history_where_clause) \
            .group_by(fn.strftime('%Y-%m-%d', TableHistory.timestamp, 'unixepoch'))\
            .dicts()
        data_series = list(data_series)

        data_movies = TableHistoryMovie.select(fn.strftime('%Y-%m-%d', TableHistoryMovie.timestamp, 'unixepoch').alias('date'),
                                               fn.COUNT(TableHistoryMovie.id).alias('count')) \
            .where(history_where_clause_movie) \
            .group_by(fn.strftime('%Y-%m-%d', TableHistoryMovie.timestamp, 'unixepoch')) \
            .dicts()
        data_movies = list(data_movies)

        for dt in rrule.rrule(rrule.DAILY,
                              dtstart=datetime.datetime.now() - datetime.timedelta(seconds=delay),
                              until=datetime.datetime.now()):
            if not any(d['date'] == dt.strftime('%Y-%m-%d') for d in data_series):
                data_series.append({'date': dt.strftime('%Y-%m-%d'), 'count': 0})
            if not any(d['date'] == dt.strftime('%Y-%m-%d') for d in data_movies):
                data_movies.append({'date': dt.strftime('%Y-%m-%d'), 'count': 0})

        sorted_data_series = sorted(data_series, key=lambda i: i['date'])
        sorted_data_movies = sorted(data_movies, key=lambda i: i['date'])

        return jsonify(series=sorted_data_series, movies=sorted_data_movies)


# GET: Get Wanted Episodes
class EpisodesWanted(Resource):
    @authenticate
    def get(self):
        episodeid = request.args.getlist('episodeid[]')

        wanted_conditions = [(TableEpisodes.missing_subtitles != '[]')]
        if len(episodeid) > 0:
            wanted_conditions.append((TableEpisodes.episodeId in episodeid))
        wanted_conditions += get_exclusion_clause('series')
        wanted_condition = reduce(operator.and_, wanted_conditions)

        if len(episodeid) > 0:
            data = TableEpisodes.select(TableShows.title.alias('seriesTitle'),
                                        TableEpisodes.monitored,
                                        TableEpisodes.season.concat('x').concat(TableEpisodes.episode).alias('episode_number'),
                                        TableEpisodes.title.alias('episodeTitle'),
                                        TableEpisodes.missing_subtitles,
                                        TableEpisodes.seriesId,
                                        TableEpisodes.episodeId,
                                        TableShows.tags,
                                        TableEpisodes.failedAttempts,
                                        TableShows.seriesType)\
                .join(TableShows)\
                .where(wanted_condition)\
                .dicts()
        else:
            start = request.args.get('start') or 0
            length = request.args.get('length') or -1
            data = TableEpisodes.select(TableShows.title.alias('seriesTitle'),
                                        TableEpisodes.monitored,
                                        TableEpisodes.season.concat('x').concat(TableEpisodes.episode).alias('episode_number'),
                                        TableEpisodes.title.alias('episodeTitle'),
                                        TableEpisodes.missing_subtitles,
                                        TableEpisodes.seriesId,
                                        TableEpisodes.episodeId,
                                        TableShows.tags,
                                        TableEpisodes.failedAttempts,
                                        TableShows.seriesType)\
                .join(TableShows)\
                .where(wanted_condition)\
                .order_by(TableEpisodes.episodeId.desc())\
                .limit(length)\
                .offset(start)\
                .dicts()
        data = list(data)

        for item in data:
            postprocessEpisode(item)

        count_conditions = [(TableEpisodes.missing_subtitles != '[]')]
        count_conditions += get_exclusion_clause('series')
        count = TableEpisodes.select(TableShows.tags,
                                     TableShows.seriesType,
                                     TableEpisodes.monitored)\
            .join(TableShows)\
            .where(reduce(operator.and_, count_conditions))\
            .count()

        return jsonify(data=data, total=count)


# GET: Get Wanted Movies
class MoviesWanted(Resource):
    @authenticate
    def get(self):
        movieid = request.args.getlist("movieid[]")

        wanted_conditions = [(TableMovies.missing_subtitles != '[]')]
        if len(movieid) > 0:
            wanted_conditions.append((TableMovies.movieId.in_(movieid)))
        wanted_conditions += get_exclusion_clause('movie')
        wanted_condition = reduce(operator.and_, wanted_conditions)

        if len(movieid) > 0:
            result = TableMovies.select(TableMovies.title,
                                        TableMovies.missing_subtitles,
                                        TableMovies.movieId,
                                        TableMovies.failedAttempts,
                                        TableMovies.tags,
                                        TableMovies.monitored)\
                .where(wanted_condition)\
                .dicts()
        else:
            start = request.args.get('start') or 0
            length = request.args.get('length') or -1
            result = TableMovies.select(TableMovies.title,
                                        TableMovies.missing_subtitles,
                                        TableMovies.movieId,
                                        TableMovies.failedAttempts,
                                        TableMovies.tags,
                                        TableMovies.monitored)\
                .where(wanted_condition)\
                .order_by(TableMovies.movieId.desc())\
                .limit(length)\
                .offset(start)\
                .dicts()
        result = list(result)

        for item in result:
            postprocessMovie(item)

        count_conditions = [(TableMovies.missing_subtitles != '[]')]
        count_conditions += get_exclusion_clause('movie')
        count = TableMovies.select(TableMovies.monitored,
                                   TableMovies.tags)\
            .where(reduce(operator.and_, count_conditions))\
            .count()

        return jsonify(data=result, total=count)


# GET: get blacklist
# POST: add blacklist
# DELETE: remove blacklist
class EpisodesBlacklist(Resource):
    @authenticate
    def get(self):
        start = request.args.get('start') or 0
        length = request.args.get('length') or -1

        data = TableBlacklist.select(TableShows.title.alias('seriesTitle'),
                                     TableEpisodes.season.concat('x').concat(TableEpisodes.episode).alias('episode_number'),
                                     TableEpisodes.title.alias('episodeTitle'),
                                     TableEpisodes.seriesId,
                                     TableBlacklist.provider,
                                     TableBlacklist.subs_id,
                                     TableBlacklist.language,
                                     TableBlacklist.timestamp)\
            .join(TableEpisodes)\
            .join(TableShows)\
            .order_by(TableBlacklist.timestamp.desc())\
            .limit(length)\
            .offset(start)\
            .dicts()
        data = list(data)

        for item in data:
            # Make timestamp pretty
            item["parsed_timestamp"] = datetime.datetime.fromtimestamp(int(item['timestamp'])).strftime('%x %X')
            item.update({'timestamp': pretty.date(datetime.datetime.fromtimestamp(item['timestamp']))})

            postprocessEpisode(item)

        return jsonify(data=data)

    @authenticate
    def post(self):
        series_id = int(request.args.get('seriesid'))
        episode_id = int(request.args.get('episodeid'))
        provider = request.form.get('provider')
        subs_id = request.form.get('subs_id')
        language = request.form.get('language')

        episodeInfo = TableEpisodes.select(TableEpisodes.path)\
            .where(TableEpisodes.episodeId == episode_id)\
            .dicts()\
            .get()

        media_path = episodeInfo['path']
        subtitles_path = request.form.get('subtitles_path')

        blacklist_log(series_id=series_id,
                      episode_id=episode_id,
                      provider=provider,
                      subs_id=subs_id,
                      language=language)
        delete_subtitles(media_type='series',
                         language=language,
                         forced=False,
                         hi=False,
                         media_path=media_path,
                         subtitles_path=subtitles_path,
                         series_id=series_id,
                         episode_id=episode_id)
        episode_download_subtitles(episode_id)
        event_stream(type='episode-history')
        return '', 200

    @authenticate
    def delete(self):
        if request.args.get("all") == "true":
            blacklist_delete_all()
        else:
            provider = request.form.get('provider')
            subs_id = request.form.get('subs_id')
            blacklist_delete(provider=provider, subs_id=subs_id)
        return '', 204


# GET: get blacklist
# POST: add blacklist
# DELETE: remove blacklist
class MoviesBlacklist(Resource):
    @authenticate
    def get(self):
        start = request.args.get('start') or 0
        length = request.args.get('length') or -1

        data = TableBlacklistMovie.select(TableMovies.title,
                                          TableMovies.movieId,
                                          TableBlacklistMovie.provider,
                                          TableBlacklistMovie.subs_id,
                                          TableBlacklistMovie.language,
                                          TableBlacklistMovie.timestamp)\
            .join(TableMovies)\
            .order_by(TableBlacklistMovie.timestamp.desc())\
            .limit(length)\
            .offset(start)\
            .dicts()
        data = list(data)

        for item in data:
            postprocessMovie(item)

            # Make timestamp pretty
            item["parsed_timestamp"] = datetime.datetime.fromtimestamp(int(item['timestamp'])).strftime('%x %X')
            item.update({'timestamp': pretty.date(datetime.datetime.fromtimestamp(item['timestamp']))})

        return jsonify(data=data)

    @authenticate
    def post(self):
        movie_id = int(request.args.get('movieid'))
        provider = request.form.get('provider')
        subs_id = request.form.get('subs_id')
        language = request.form.get('language')
        # TODO
        forced = False
        hi = False

        data = TableMovies.select(TableMovies.path).where(TableMovies.movieId == movie_id).dicts().get()

        media_path = data['path']
        subtitles_path = request.form.get('subtitles_path')

        blacklist_log_movie(movie_id=movie_id,
                            provider=provider,
                            subs_id=subs_id,
                            language=language)
        delete_subtitles(media_type='movie',
                         language=language,
                         forced=forced,
                         hi=hi,
                         media_path=media_path,
                         subtitles_path=subtitles_path,
                         movie_id=movie_id)
        movies_download_subtitles(movie_id)
        event_stream(type='movie-history')
        return '', 200

    @authenticate
    def delete(self):
        if request.args.get("all") == "true":
            blacklist_delete_all_movie()
        else:
            provider = request.form.get('provider')
            subs_id = request.form.get('subs_id')
            blacklist_delete_movie(provider=provider, subs_id=subs_id)
        return '', 200


class Subtitles(Resource):
    @authenticate
    def patch(self):
        action = request.args.get('action')

        language = request.form.get('language')
        subtitles_path = request.form.get('path')
        media_type = request.form.get('type')
        id = request.form.get('id')

        if media_type == 'episode':
            metadata = TableEpisodes.select(TableEpisodes.path, TableEpisodes.seriesId)\
                .where(TableEpisodes.episodeId == id)\
                .dicts()\
                .get()
            video_path = metadata['path']
        else:
            metadata = TableMovies.select(TableMovies.path).where(TableMovies.movieId == id).dicts().get()
            video_path = metadata['path']

        if action == 'sync':
            if media_type == 'episode':
                subsync.sync(video_path=video_path, srt_path=subtitles_path,
                             srt_lang=language, media_type='series', series_id=metadata['seriesId'],
                             episode_id=int(id))
            else:
                subsync.sync(video_path=video_path, srt_path=subtitles_path,
                             srt_lang=language, media_type='movies', movie_id=id)
        elif action == 'translate':
            dest_language = language
            forced = True if request.form.get('forced') == 'true' else False
            hi = True if request.form.get('hi') == 'true' else False
            result = translate_subtitles_file(video_path=video_path, source_srt_file=subtitles_path,
                                              to_lang=dest_language,
                                              forced=forced, hi=hi)
            if result:
                if media_type == 'episode':
                    store_subtitles(video_path)
                else:
                    store_subtitles_movie(video_path)
                return '', 200
            else:
                return '', 404
        else:
            subtitles_apply_mods(language, subtitles_path, [action])

        # apply chmod if required
        chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
            'win') and settings.general.getboolean('chmod_enabled') else None
        if chmod:
            os.chmod(subtitles_path, chmod)

        return '', 204


class SubtitleNameInfo(Resource):
    @authenticate
    def get(self):
        names = request.args.getlist('filenames[]')
        results = []
        for name in names:
            opts = dict()
            opts['type'] = 'episode'
            guessit_result = guessit(name, options=opts)
            result = {}
            result['filename'] = name
            if 'subtitle_language' in guessit_result:
                result['subtitle_language'] = str(guessit_result['subtitle_language'])

            if 'episode' in guessit_result:
                result['episode'] = int(guessit_result['episode'])
            else:
                result['episode'] = 0

            if 'season' in guessit_result:
                result['season'] = int(guessit_result['season'])
            else:
                result['season'] = 0

            results.append(result)

        return jsonify(data=results)


class BrowseBazarrFS(Resource):
    @authenticate
    def get(self):
        path = request.args.get('path') or ''
        data = []
        try:
            result = browse_bazarr_filesystem(path)
            if result is None:
                raise ValueError
        except Exception:
            return jsonify([])
        for item in result['directories']:
            data.append({'name': item['name'], 'children': True, 'path': item['path']})
        return jsonify(data)


class WebHooksPlex(Resource):
    @authenticate
    def post(self):
        json_webhook = request.form.get('payload')
        parsed_json_webhook = json.loads(json_webhook)

        event = parsed_json_webhook['event']
        if event not in ['media.play']:
            return '', 204

        media_type = parsed_json_webhook['Metadata']['type']

        if media_type == 'episode':
            season = parsed_json_webhook['Metadata']['parentIndex']
            episode = parsed_json_webhook['Metadata']['index']
        else:
            season = episode = None

        ids = []
        for item in parsed_json_webhook['Metadata']['Guid']:
            splitted_id = item['id'].split('://')
            if len(splitted_id) == 2:
                ids.append({splitted_id[0]: splitted_id[1]})
        if not ids:
            return '', 404

        if media_type == 'episode':
            try:
                episode_imdb_id = [x['imdb'] for x in ids if 'imdb' in x][0]
                r = requests.get('https://imdb.com/title/{}'.format(episode_imdb_id),
                                 headers={"User-Agent": os.environ["SZ_USER_AGENT"]})
                soup = bso(r.content, "html.parser")
                series_imdb_id = soup.find('a', {'class': re.compile(r'SeriesParentLink__ParentTextLink')})['href'].split('/')[2]
            except Exception:
                return '', 404
            else:
                episodeId = TableEpisodes.select(TableEpisodes.episodeId) \
                    .join(TableShows) \
                    .where(TableShows.imdbId == series_imdb_id,
                           TableEpisodes.season == season,
                           TableEpisodes.episode == episode) \
                    .dicts() \
                    .get()

                if episodeId:
                    episode_download_subtitles(no=episodeId['episodeId'], send_progress=True)
        else:
            try:
                movie_imdb_id = [x['imdb'] for x in ids if 'imdb' in x][0]
            except Exception:
                return '', 404
            else:
                movieId = TableMovies.select(TableMovies.movieId)\
                    .where(TableMovies.imdbId == movie_imdb_id)\
                    .dicts()\
                    .get()
                if movieId:
                    movies_download_subtitles(no=movieId['movieId'])

        return '', 200


api.add_resource(Badges, '/badges')

api.add_resource(Providers, '/providers')
api.add_resource(ProviderMovies, '/providers/movies')
api.add_resource(ProviderEpisodes, '/providers/episodes')

api.add_resource(System, '/system')
api.add_resource(Searches, "/system/searches")
api.add_resource(SystemAccount, '/system/account')
api.add_resource(SystemTasks, '/system/tasks')
api.add_resource(SystemLogs, '/system/logs')
api.add_resource(SystemStatus, '/system/status')
api.add_resource(SystemHealth, '/system/health')
api.add_resource(SystemReleases, '/system/releases')
api.add_resource(SystemSettings, '/system/settings')
api.add_resource(Languages, '/system/languages')
api.add_resource(LanguagesProfiles, '/system/languages/profiles')
api.add_resource(Notifications, '/system/notifications')

api.add_resource(Subtitles, '/subtitles')
api.add_resource(SubtitleNameInfo, '/subtitles/info')

api.add_resource(Series, '/series')
api.add_resource(SeriesRootfolders, '/series/rootfolders')
api.add_resource(SeriesDirectories, '/series/directories')
api.add_resource(SeriesLookup, '/series/lookup')
api.add_resource(SeriesAdd, '/series/add')
api.add_resource(SeriesModify, '/series/modify')

api.add_resource(Episodes, '/episodes')
api.add_resource(EpisodesWanted, '/episodes/wanted')
api.add_resource(EpisodesSubtitles, '/episodes/subtitles')
api.add_resource(EpisodesHistory, '/episodes/history')
api.add_resource(EpisodesBlacklist, '/episodes/blacklist')

api.add_resource(Movies, '/movies')
api.add_resource(MoviesWanted, '/movies/wanted')
api.add_resource(MoviesSubtitles, '/movies/subtitles')
api.add_resource(MoviesHistory, '/movies/history')
api.add_resource(MoviesBlacklist, '/movies/blacklist')
api.add_resource(MoviesRootfolders, '/movies/rootfolders')
api.add_resource(MoviesDirectories, '/movies/directories')
api.add_resource(MoviesLookup, '/movies/lookup')
api.add_resource(MoviesAdd, '/movies/add')
api.add_resource(MoviesModify, '/movies/modify')

api.add_resource(HistoryStats, '/history/stats')

api.add_resource(BrowseBazarrFS, '/files')

api.add_resource(WebHooksPlex, '/webhooks/plex')
