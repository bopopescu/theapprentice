import urllib2, urllib
import socket
from urllib2 import URLError, HTTPError
from datetime import datetime
import re, time
import json
import hashlib
import base64
import traceback
import xbmcgui, xbmc
from dudehere.routines import *
from dudehere.routines import plugin
from dudehere.routines.omdbapi import OMDBapi
from dudehere.routines.constants import WINDOW_ACTIONS
from dudehere.routines import fanart

REDIRECT_URI = 'urn:ietf:wg:oauth:2.0:oob'
BASE_URL = "https://api.trakt.tv" #"http://api-v2launch.trakt.tv"
CLIENT_ID = plugin.get_setting('trakt_client_id')
SECRET_ID = plugin.get_setting('trakt_secret')
PIN_URL = plugin.get_setting('trakt_pin_url')
DAYS_TO_GET = 21
DECAY = 2
omdb = OMDBapi()
SHOW_UNAIRED_EPISODES = plugin.get_setting('show_unaired_episodes')
#_ADDON = MyAddon('script.module.dudehere.routines', ARGV)
DB_VERSION=15
if plugin.get_setting('database_type', addon_id='script.module.dudehere.routines')=='1':
	DB_NAME = plugin.get_setting('database_mysql_name', addon_id='script.module.dudehere.routines')
	DB_USER = plugin.get_setting('database_mysql_user', addon_id='script.module.dudehere.routines')
	DB_PASS = plugin.get_setting('database_mysql_pass',addon_id='script.module.dudehere.routines')
	DB_PORT = plugin.get_setting('database_mysql_port',addon_id='script.module.dudehere.routines')
	DB_ADDRESS = plugin.get_setting('database_mysql_host', addon_id='script.module.dudehere.routines')
	DB_TYPE = 'mysql'
	from dudehere.routines.database import MySQLDatabase as DatabaseAPI
	DATE_FILTER = "?"
else:
	DB_TYPE = 'sqlite'
	DB_LOCATION = vfs.join('special://userdata', 'addon_data/script.module.dudehere.routines')
	if not vfs.exists(DB_LOCATION): vfs.mkdir(DB_LOCATION)
	DB_FILE = xbmc.translatePath(plugin.get_setting('database_sqlite_file', addon_id='script.module.dudehere.routines'))
	from dudehere.routines.database import SQLiteDatabase as DatabaseAPI
	DATE_FILTER = "DATETIME(?)"

class MyDatabaseAPI(DatabaseAPI):
	def _initialize(self):
		import xbmcaddon
		root = xbmcaddon.Addon('script.module.dudehere.routines').getAddonInfo('path')
		schema_file = vfs.join(root, 'resources/database/trakt.schema.%s.sql' % self.db_type)

		if self.run_script(schema_file, commit=False):
			self.execute('DELETE FROM version WHERE 1')
			self.execute('INSERT INTO version(db_version) VALUES(?)', [self.db_version])
			self.commit()

	def do_init(self):
		do_init = True
		try:
			test = self.query("SELECT 1 FROM version WHERE db_version >= ?", [self.db_version], silent=True)
			if test:
				do_init = False
		except:
			do_init = True
		return do_init

if DB_TYPE == 'mysql':
	DB=MyDatabaseAPI(DB_ADDRESS, DB_NAME, DB_USER, DB_PASS, DB_PORT, version=DB_VERSION, connect=True, quiet=True)
else:
	DB = MyDatabaseAPI(DB_FILE, version=DB_VERSION, connect=True, quiet=True)

fanart.DB = DB
DISABLE_FANART = plugin.get_setting('enable_fanart') == "false"
DISABLE_FANART_SERIES = plugin.get_setting('enable_series_fanart') == "false"
DISABLE_FANART_EPISODES = plugin.get_setting('enable_episode_fanart') == "false"
DISABLE_FANART_MOVIES = plugin.get_setting('enable_movie_fanart') == "false"
FANART_AUTOSEARCH = plugin.get_setting('enable_fanart_autosearch')
if not FANART_AUTOSEARCH:
	FANART_AUTOSEARCH = 0


class TraktError(Exception):
	def __init__(self, value):
		self.value = value
	def __str__(self):
		try:
			s = self.value
		except Exception,e:
			print "-----",type(e),e
		return s
	
class TraktTempError(Exception):
	pass

class AuthWindow(xbmcgui.WindowXMLDialog):
		message = False
		def __init__(self, *args, **kwargs):
				xbmcgui.WindowXML.__init__(self)
			
		def onInit(self):
			self.response = False
			self.getControl(82004).setVisible(False)
			if self.message:
				self.getControl(82002).setLabel(self.message)

		def onAction(self, action):
			action = action.getId()
			if action in [WINDOW_ACTIONS.ACTION_PREVIOUS_MENU, WINDOW_ACTIONS.ACTION_NAV_BACK]:
				self.close()
			
			try:
				if action in [WINDOW_ACTIONS.ACTION_SHOW_INFO, WINDOW_ACTIONS.ACTION_CONTEXT_MENU]:
					controlID = self.getFocus().getId()
			except:
				pass
			
		def onClick(self, controlID):
			trakt = TraktAPI()
			if controlID in [82000]:
				plugin.set_property('Abort', "true")
				self.close()
			if controlID == 82001:
				plugin.set_property('Abort', "false")
				self.getControl(82004).setVisible(True)
				self.response = trakt._authorize(window=self)
				if self.response:
					self.close()

		def onFocus(self, controlID):
			pass


class TraktAPI():
	def __init__(self):
		self.token = None
		self.timeout = 30
		self.limit = 100
	
	def authorize(self, window):
		response = self._authorize(window=window)
		return response
	
	def get_settings(self):
		uri = '/users/settings'
		settings = self._call(uri, auth=True)
		if not settings: return False
		return settings
	
	def get_object_id(self, trakt_id = None, imdb_id=None, tmdb_id=None, id_type='trakt'):
		if trakt_id is not None: return trakt_id
		if imdb_id is not None: return imdb_id
		uri = '/search'
		result = self._call(uri, params={"id_type": 'tmdb_id', "id": tmdb_id})
		if not result: return False
		type = result[0]['type']
		return result[0][type]['ids']['id_type']
	
	def resolve_imdb_id(self, title, year, media, tmdb_id='', trakt_id=''):
		imdb_id = None
		try:
			id = DB.query("SELECT imdb_id FROM id_cache WHERE title=? AND media=? AND year=?", [title, media, year])
			if id:
				imdb_id = id[0]
			else:
				imdb_id = omdb.query_id(title, year, media)
				if imdb_id is not None:
					DB.execute("INSERT INTO id_cache(title, year, media, imdb_id, tmdb_id, trakt_id) VALUES(?,?,?,?,?,?)", [title, year, media, imdb_id, tmdb_id, trakt_id])
					DB.commit()
		except Exception, e:
			plugin.log(e)
		return imdb_id
		
	def query_id(self, id_type, id, media=None):
		uri = '/search/%s/%s' %( id_type, id)
		
		if media is not None:
			result = self._call(uri, params={"type": media}, no_limit=True, cache_limit=0)
		else:
			result = self._call(uri, no_limit=True, cache_limit=0)
		if not result: return False
		type = result[0]['type']
		return result[0][type]['ids']['imdb']
	
	def translate_id(self, id, from_type, to_type, media):
		uri = '/search/%s/%s' %( from_type, id)
		params = {}
		if media is not None:
			params['type'] = media
		response = self._call(uri, no_limit=True, cache_limit=False)
		if not response: return False
		ids = response[0][media]['ids']
		return ids[to_type]
			
	
	def query_slug(self, id_type, id):
		uri = '/search/%s/%s' %( id_type, id)
		result = self._call(uri, no_limit=True)
		if not result: return False
		type = result[0]['type']
		return result[0][type]['ids']['slug']
	
	def lookup(self, media, id, id_type='imdb'):
		uri = '/search'
		record = self._call(uri, params={"id_type": id_type, "id": id, 'extended': 'full,images'})
		if not record: return False
		metadata = self.process_record(record, media)
		return metadata
	
	def get_genres(self, media):
		uri = "/genres/%s" % media
		return self._call(uri, cache_limit=86600)
	
	def get_episode_details(self, id, season, episode, params={'extended': 'full,images'}):
		uri = '/shows/%s/seasons/%s/episodes/%s' % (id, season, episode)
		episode = self._call(uri, params=params, auth=False, cache_limit=0)
		show = self.get_show_details(id)
		record = {"episode": episode, "show": show}
		return self.process_record(record, media='episode')
	
	def get_show_details(self, id):
		uri = '/shows/%s' % id
		record = self._call(uri, params={'extended': 'full,images'}, auth=False, cache_limit=0)
		return record
	
	def get_movie_details(self, id):
		uri = '/movies/%s' % (id)	
		record = self._call(uri, params={'extended': 'full,images'}, auth=False, cache_limit=0)
		if not record: return False
		return self.process_record(record, media='movie')
	
	def get_metadata(self, media, imdb_id, tmdb_id, trakt_id, slug, season=None, episode=None):
		metadata = None
		if media == 'episode':
			return self.get_episode_metadata(imdb_id, tmdb_id, trakt_id, slug, season, episode)
		elif media == 'tvshow':
			return self.get_show_metadata(imdb_id, tmdb_id, trakt_id, slug)
		else:
			return self.get_movie_metadata(imdb_id, tmdb_id, trakt_id, slug)
	
	def get_episode_metadata(self, imdb_id, tmdb_id, trakt_id, slug, season, episode):
		metadata = None
		cached = False
		SQL = "SELECT metadata FROM metadata_episodes where trakt_id=?"
		cached = DB.query(SQL, [trakt_id])
		if cached:
			cached = plugin.unpickle(cached[0])
			cached['cast'] = []
			plugin.log('Loading cached metadata')
			return cached
		
		plugin.log('Requesting metadata')
		if slug:
			return self.get_episode_details(slug, season, episode)
		if trakt_id:
			return self.get_episode_details(trakt_id, season, episode)
		if imdb_id:
			return self.get_episode_details(imdb_id, season, episode)
		if tmdb_id:
			imdb_id = self.query_id('tmdb', tmdb_id)
			return self.get_episode_details(imdb_id, season, episode)
		return metadata
	
	def get_show_metadata(self, imdb_id, tmdb_id, trakt_id, slug):
		metadata = None
		cached = False
		SQL = "SELECT metadata FROM metadata_shows where trakt_id=?"
		cached = DB.query(SQL, [trakt_id])
			
		if cached:
			cached = plugin.unpickle(cached[0])
			cached['cast'] = []
			plugin.log('Loading cached metadata')
			return cached
		plugin.log('Requesting metadata')
		if slug:
			return self.get_show_details(slug)
		if imdb_id:
			return self.get_show_details(imdb_id)
		if tmdb_id:
			imdb_id = self.query_id('tmdb', tmdb_id)
			return self.get_show_details(imdb_id)
		if trakt_id:
			return self.get_show_details(trakt_id)
		return metadata
	
	def get_movie_metadata(self, imdb_id, tmdb_id, trakt_id, slug):
		metadata = None
		cached = False
		SQL = "SELECT metadata FROM metadata_movies where trakt_id=?"
		cached = DB.query(SQL, [trakt_id])
			
		if cached:
			cached = plugin.unpickle(cached[0])
			cached['cast'] = []
			plugin.log('Loading cached metadata')
			return cached
		plugin.log('Requesting metadata')
		if slug:
			return self.get_movie_details(slug)
		if imdb_id:
			return self.get_movie_details(imdb_id)
		if tmdb_id:
			imdb_id = self.query_id('tmdb', tmdb_id)
			return self.get_movie_details(imdb_id)
		if trakt_id:
			return self.get_movie_details(trakt_id)
		return metadata
		
	def search(self, query, media='show'):
		uri = '/search'
		return self._call(uri, params={'query': query, 'type': media, 'extended': 'full,images'}, cache_limit=86600)
	
	def advanced_search(self, media, filters):
		params={'type': media, 'extended': 'full,images'}
		if filters['query'] == '' and media=='show':
			uri = '/shows/popular'
		elif filters['query'] == '':
			uri = '/movies/popular'
		else:
			uri = '/search'
			params['query'] = filters['query']

			
		if media == 'show':
			params['genres'] = ','.join(filters['genres']).replace(" ", "-").lower()
			params['certifications'] = ','.join(filters['certifications']).lower()
			params['networks'] = ','.join(filters['networks'])
		else:
			params['genres'] = ','.join(filters['genres']).replace(" ", "-").lower()
			params['certifications'] = ','.join(filters['certifications']).lower()
			params['years'] = filters['year']
			
		return self._call(uri, params=params, cache_limit=86600)	
		
	def get_calendar_shows(self):
		from datetime import date, timedelta
		d = date.today() - timedelta(days=(DAYS_TO_GET - 1)) 
		today = d.strftime("%Y-%m-%d")
		uri = '/calendars/my/shows/%s/%s' % (today, DAYS_TO_GET)
		media='episode'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=True)
	
	def get_calendar_daily_shows(self, delta=0, number=1):
		from datetime import date, timedelta
		d = date.today() - timedelta(days=delta)
		start_date = d.strftime("%Y-%m-%d")
		uri = '/calendars/my/shows/%s/%s' % (start_date, number)
		media='episode'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=True)
	
	def get_calendar_episodes(self, delta=0, number=1):
		from datetime import date, timedelta
		d = date.today() - timedelta(days=delta)
		start_date = d.strftime("%Y-%m-%d")
		uri = '/calendars/all/shows/%s/%s' % (start_date, number)
		media='episode'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=False)
	
	def get_calendar(self, calendar, delta=0, number=1):
		from datetime import date, timedelta
		d = date.today() - timedelta(days=delta)
		start_date = d.strftime("%Y-%m-%d")
		uri = '/calendars/%s/%s/%s' % (calendar, start_date, number)
		media='episode'
		auth = calendar.startswith("my")
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=auth)
		
	def get_similar_tvshows(self, id):
		uri = '/shows/%s/related' % id
		media = 'show'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=86600, auth=False)
	
	def get_collected_tvshows(self):
		uri = '/sync/collection/shows'
		media='show'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=True)
	
	def get_trending_tvshows(self):
		uri = '/shows/trending'
		media='show'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=False)
	
	def get_tvshow_genre(self, genre):
		uri = '/shows/trending'
		media='show'
		return self._call(uri, params={'extended': 'full,images', "genres": genre}, cache_limit=60, auth=False)
	
	def get_anticipated_tvshows(self):
		uri = '/shows/anticipated'
		media='show'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=False)
	
	def get_popular_tvshows(self):
		uri = '/shows/popular'
		media='show'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=False)
	
	def get_recommended_tvshows(self):
		uri = '/recommendations/shows'
		media='show'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=True)
	
	def get_show_seasons(self, id, tvdb_id="", params={'extended': 'images'}, cache_limit=86600):
		uri = '/shows/%s/seasons' % id
		media='season'
		results = self._call(uri, params=params, cache_limit=cache_limit)
		if tvdb_id:
			season_art = fanart.get_season_art(tvdb_id, id)
			for r in results:
				season = r['number']
				if season in season_art:
					results[results.index(r)]['poster'] = season_art[season]
				else:
					results[results.index(r)]['poster'] = SHARED_ARTWORK + "no_poster.jpg"
		return results
	
	def get_show_episodes(self, id, season, params={'extended': 'full,images'}, cache_limit=86600):
		uri = '/shows/%s/seasons/%s' % (id, season)
		media='episode'
		return self._call(uri, params=params, cache_limit=cache_limit)
	
	def get_episodes_ondeck(self, slug=None):
		ondeck = []
		shows = False
		if slug is None or slug=='watchlist':
			shows = self.get_watchlist_tvshows()
		else:
			shows = self.get_custom_list(slug, 'tvshows',  params={'extended': ''})
		if not shows: return []
		
		for show in shows:
			slug = show['show']['ids']['slug']
			imdb_id = show['show']['ids']['imdb']
			tmdb_id = show['show']['ids']['tmdb']
			trakt_id = show['show']['ids']['trakt']
			next = self.get_next_episode(slug)
			if next:
				season = next['season']
				episode = next['number']
				if season == 0 or episode == 0: continue
				episode = self.get_episode_metadata(imdb_id, tmdb_id, trakt_id, slug, season, episode)
				ondeck.append( episode )
		return ondeck

	
	
	def get_next_episode(self, slug):
		uri = '/shows/%s/progress/watched' % slug
		result = self._call(uri, params={"hidden": "false", "specials":"false"}, auth=True)
		if result['next_episode'] is None:
			return False
		return result['next_episode']

	
	def get_watchlist_tvshows(self, extended='full,images', simple=False, id_type='imdb'):
		uri = '/users/me/watchlist/shows'
		a = self.check_activities()
		if simple:
			records = self.cache_request(a['shows']['watchlisted_at'], 'shows_watchlisted_at_simple', uri, auth=True)
			if not records: return {id_type: []}
			return [record['show']['ids'][id_type] for record in records] + [record['show']['ids']['slug'] for record in records]
		else:
			results = self.cache_request(a['shows']['watchlisted_at'], 'shows_watchlisted_at', uri, params={'extended': extended}, auth=True)
			return results
	
	def get_watchlist_movies(self, extended='full,images', simple=False, id_type='imdb'):
		uri = '/users/me/watchlist/movies'
		a = self.check_activities()
		if simple:
			records = self.cache_request(a['movies']['watchlisted_at'], 'movies_watchlisted_at_simple', uri, auth=True)
			if not records: return {id_type: []}
			return [record['movie']['ids'][id_type] for record in records]  + [record['movie']['ids']['slug'] for record in records]
		else:
			results = self.cache_request(a['movies']['watchlisted_at'], 'movies_watchlisted_at', uri, params={'extended': extended}, auth=True)
			return results
			
	def get_collected_movies(self):
		uri = '/sync/collection/movies'
		media='movie'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=True)
	
	def get_trending_movies(self):
		uri = '/movies/trending'
		media='movie'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=False)
	
	def get_popular_movies(self):
		uri = '/movies/popular'
		media = 'movie'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=False)
	
	def get_recommended_movies(self):
		uri = '/recommendations/movies'
		media = 'movie'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=60, auth=True)
	
	def get_similar_movies(self, id):
		uri = '/movies/%s/related' % id
		media = 'movie'
		return self._call(uri, params={'extended': 'full,images'}, cache_limit=86600, auth=False)
	
	def get_show_info(self, id, episodes=False):
		if episodes:
			uri = '/shows/%s/seasons' % id
			return self._call(uri, params={'extended': 'episodes,full'}, cache_limit=86600)
		else:
			uri = '/shows/%s' % id
			return self._call(uri, cache_limit=86600)
	
	def get_cast(self, id, media):
		uri = "/%s/%s/people" % (id, media)
		response = self._call(uri, params={'extended': 'full'}, auth=True, cache_limit=86600)
		plugin.log(response)
	
	def get_custom_lists(self):
		uri = '/users/me/lists'
		results = self._call(uri, params={}, auth=True)
		if not results: return False
		return sorted(results)
	
	def get_liked_lists(self):
		uri = '/users/likes/lists'
		return sorted(self._call(uri, params={}, auth=True))
	
	def get_custom_list(self, slug, media, username=None, params={'extended': 'full,images'}, cache_limit=0):
		if media=='tvshows': media = 'show'
		if media=='tv': media = 'show'
		if username is None:	
			uri = '/users/me/lists/%s/items' % slug 
			auth = True
		else:
			uri = '/users/%s/lists/%s/items' % (username.replace('.', '-'), slug)
			auth = False
		temp = self._call(uri, params=params, auth=auth, cache_limit=cache_limit)
		results = []
		if not temp: return results
		for r in temp:
			if r['type'] == media:
				results.append(r)
		return results		
	
	def create_custom_list(self, title):
		uri = '/users/me/lists'
		post_dict = {
			"name": title,
			"description": "Created by %s" % ADDON_NAME,
			"privacy": "private",
			"display_numbers": True,
			"allow_comments": True
		}
		return self._call(uri, data=post_dict, auth=True)
	
	def add_to_watchlist(self, media, id, id_type='imdb'):
		uri = '/sync/watchlist'
		data = {media:  [{'ids': {id_type: id}}]}
		return self._call(uri, data, auth=True)
	
	def delete_from_watchlist(self, media, id, id_type='imdb'):
		uri = '/sync/watchlist/remove'
		data = {media:  [{'ids': {id_type: id}}]}
		return self._call(uri, data, auth=True)
		
	def add_to_custom_list(self, media, slug, id, id_type='imdb'):
		if media=='movie':
			post_dict = {'movies': [{'ids': {id_type: id}}]}
		else:
			post_dict = {'shows': [{'ids': {id_type: id}}]}
		uri = '/users/me/lists/%s/items' % slug
		return self._call(uri, data=post_dict, auth=True)
	
	def delete_from_custom_list(self, media, slug, id, id_type='imdb'):
		if media=='movie':
			post_dict = {'movies': [{'ids': {id_type: id}}]}
		else:
			post_dict = {'shows': [{'ids': {id_type: id}}]}
		uri = '/users/me/lists/%s/items/remove' % slug
		return self._call(uri, post_dict, auth=True)
	
	def delete_custom_list(self, slug):
		uri = '/users/me/lists/%s' % slug
		return self._delete(uri)
	
	def toggle_sync_state(self, name, slug):
		test = DB.query("SELECT 1 FROM sync_states WHERE slug=?", [slug])
		if test:
			DB.execute("UPDATE sync_states SET sync=ABS(sync - 1) WHERE slug=?", [slug])
		else:
			DB.execute("INSERT INTO sync_states(name, slug) VALUES (?,?)", [name, slug])
		DB.commit()
	
	def set_playback_addon(self, name, slug, addon_id):
		test = DB.query("SELECT 1 FROM sync_states WHERE slug=?", [slug])
		if test:
			DB.execute("UPDATE sync_states SET addon=? WHERE slug=?", [addon_id, slug])
		else:
			DB.execute("INSERT INTO sync_states(name, slug, addon,sync) VALUES (?,?,?,0)", [name, slug, addon_id])
		DB.commit()
		
		
	def get_sync_state(self, slug):
		test = DB.query("SELECT sync FROM sync_states WHERE slug=?", [slug])
		if test and test[0] == 1:
			return True
		else:
			return False
	
	def get_sync_lists(self):
		return DB.query_assoc("SELECT name, slug, addon as addon_id FROM sync_states WHERE sync=1", force_double_array=True)
		
	def set_watched_state(self, media, id, watched, season=None, id_type='imdb'):
		uri = '/sync/history' if watched else '/sync/history/remove'
		if media == 'episode':
			post_dict = {'episodes': [{"ids": {"trakt": id}}]}
		elif media == 'movie':
			post_dict = {'movies': [{"ids": {id_type: id}}]}
		elif media == 'season':
			post_dict = {'shows': [{'seasons': [{'number': int(season)}], 'ids': {id_type: id}}]}
		return self._call(uri, post_dict, auth=True)
	
	def get_watched_history(self, media):
		uri = '/sync/watched/%s' % media
		if media == 'shows':
			results = {}
			response = self._call(uri, auth=True)
			if not response: return False
			for r in response:
				imdb_id =  r['show']['ids']['imdb']
				results[imdb_id] = {}
				seasons = r['seasons']
				for season in seasons:
					results[imdb_id][season['number']] = []
					for episode in season['episodes']: results[imdb_id][season['number']].append(episode['number'])
			return results
		else:
			results = {"imdb": [], "tmdb": [], "trakt": []}
			response = self._call(uri, auth=True)
			if not response: return False
			for r in response:
				results['trakt'].append(r['movie']['ids']['trakt'])
				results['imdb'].append(r['movie']['ids']['imdb'])
				results['tmdb'].append(r['movie']['ids']['tmdb'])
			return results	

	def get_show_progress(self, show, fresh):
		record = {"ids": show['show']["ids"], "title": show['show']["title"], "progress": {"show": {"total": 0, "watched": 0, "percent": 0}}}
		seasons = self.get_show_seasons(show['show']['ids']['imdb'], '', {}, cache_limit=False)
		season_count = 0
		full = {}
		for season in seasons:
			season_number = season['number']
			if season['number'] == 0: continue
			record['progress'][season_number] = {"watched": 0, "percent": 0}
			
			full[season_number] = self.get_show_episodes(show['show']['ids']['trakt'], season_number, cache_limit=False)
			episode_count = 0
			for e in full[season_number]:
				if e['number'] > 0:
					if self.check_air_date(str(e['first_aired'])): continue
					episode_count += 1
			record['progress'][season_number]['total'] = episode_count
			record['progress']['show']['total'] += episode_count
			season_count += 1
			
		record['season_count'] = season_count
		uri = uri = '/sync/history/shows/%s' % show['show']['ids']['trakt']
		if fresh:
			history = self._call(uri, auth=True, cache_limit=86600)
		else:
			history = self._call(uri, auth=True, cache_limit=False)
		if len(history) > 0:
			count = {}
			for h in history:
				if h['episode']['season'] == 0 or h['episode']['number'] == 0: continue
				if h['episode']['season'] not in count: count[ h['episode']['season']] = []
				count[ h['episode']['season'] ].append( h['episode']['number'])
			for season in count:
				record['progress'][season]["watched"] = len(set(count[season]))
				percent = float(record['progress'][season]["watched"]) / float(record['progress'][season]["total"]) if record['progress'][season]["total"] > 0 else 0
				record['progress'][season]["percent"] = percent
				record['progress']['show']['watched'] += record['progress'][season]["watched"]
				
			record['progress']['show']['percent'] = float(record['progress']['show']['watched']) / float(record['progress']['show']['total']) if record['progress']['show']['total'] > 0 else 0
		record['progress']['next'] = self.get_next_episode(show['show']['ids']['slug'])
		record['episodes'] = full
		show.update(record)
		self.__progress_results.append(show)
		return show
	
	def check_air_date(self, air_date):
		tmp = re.match('^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.000Z', air_date)
		if tmp:
			aired = datetime(int(tmp.group(1)), int(tmp.group(2)),int(tmp.group(3)),int(tmp.group(4)),int(tmp.group(5)),int(tmp.group(6)))
			aired = time.mktime(aired.timetuple())
			now = time.mktime(datetime.now().timetuple())
			return aired > now
		return True
	
	def get_progress(self, slug):
		import threading
		self.__progress_results = []
		test = self.check_activity('lists', 'updated_at')

		if not test['updated_at'][0]:
			if DB.db_type == 'mysql':
				SQL = "REPLACE INTO activities(activity, ts) VALUES (?, unix_timestamp(?))"
			else:
				SQL = "INSERT OR REPLACE INTO activities(activity, ts) VALUES (?, strftime('%s',?))"
			DB.execute(SQL, ['lists_updated_at', test['updated_at'][1]])
			DB.commit()
		test2 = self.check_activity('episodes', 'watched_at')
		if not test2['watched_at'][0]:
			if DB.db_type == 'mysql':
				SQL = "REPLACE INTO activities(activity, ts) VALUES (?, unix_timestamp(?))"
			else:
				SQL = "INSERT OR REPLACE INTO activities(activity, ts) VALUES (?, strftime('%s',?))"
			DB.execute(SQL, ['episodes_watched_at', test2['watched_at'][1]])
			DB.commit()
			
		fresh = test['updated_at'][0] and test2['watched_at'][0]

		if fresh:
			shows = self.get_custom_list(slug, 'tvshows', cache_limit=86600)
		else:
			shows = self.get_custom_list(slug, 'tvshows')
		
		
		for show in shows:
			t = threading.Thread(target=self.get_show_progress, args=(show, False))
			t.start()
		
		main_thread = threading.currentThread()
		for t in threading.enumerate():
			if t is main_thread: continue
			t.join()
		
		self._cache_result(self.__progress_results, 'get_progres::'+slug, cache_limit=86600)	
		return self.__progress_results
		
	
	def get_next_episodes(self):
		uri = '/sync/history/episodes'

		response = self._call(uri, auth=True)
		if not response: return False
		for r in response:
			plugin.log(r)
		#plugin.log(response)
	
	def get_watched_episodes(self, id):
		a = self.check_activities()
		uri = '/sync/history/seasons/%s' % id
		results = self.cache_request(a['episodes']['watched_at'], 'episodes_watched_at_%s' % id , uri, auth=True)
		return results
	
	def get_watched_season(self, imdb_id, season, season_id):
		watched = [] 			# Collect watched episodes, discarding duplicates
		for test in self.get_watched_episodes(season_id):
			if test['episode']['number'] not in watched: watched.append(test['episode']['number'])
		watched = len(watched)
		episodes = 0
		for test in self.get_show_episodes(imdb_id, season):
			if test['number'] == 0: break
			if test['first_aired'] is None: break
			episodes += 1

		return watched == episodes if episodes > 0 else None
	
	def get_bookmark(self, media, id, id_type = 'imdb', season=None, episode=None):
		bookmarks = self.get_bookmarks(media)
		for bookmark in bookmarks:
			if media == 'episodes':
				if id== bookmark['show']['ids'][id_type] and bookmark['episode']['season'] == int(season) and bookmark['episode']['number'] == int(episode):
					return bookmark['progress']
			else:
				if id == bookmark['movie']['ids'][id_type]:
					return bookmark['progress']
		return None
	def get_bookmarks(self, media):
		uri = '/sync/playback/%s' % media
		return self._call(uri, auth=True)
		
	
	def process_record(self, record, media=None, watched=None, show=None):
		if media=='movie':
			meta = self.process_movie(record)
			return meta
		elif media=='episode':
			meta = self.process_episode(record, watched=watched, show=show)
			return meta
		elif media=='tvshow':
			meta = self.process_show(record)
			return meta
	
	def meta_map(self, path, object, default=''):
		try:
			if isinstance(path, list):
				for k in path:
					object = object[k]
			else:
				object = object[path]
				object = object if object is not None else default
			return object
		except:
			return default
	
	def format_trailer(self, trailer_url):
		if not trailer_url: return trailer_url
		match = re.search('\?v=(.*)', trailer_url)
		if match:
			return 'plugin://plugin.video.youtube/?action=play_video&videoid=%s' % (match.group(1))	
	
	def process_show(self, record):
		meta = {}
		try:
			show = record['show']
		except:
			show = record	
		trakt_id = self.meta_map(['ids', 'trakt'], show)
		if 'updated_at' not in show:
			updated_at = datetime.now()
		else:
			updated_at = show['updated_at'].replace("Z", "")
		SQL ="SELECT metadata_shows.metadata, fresh_fanart.fresh AS fresh FROM metadata_shows " \
			" LEFT JOIN fresh_fanart ON metadata_shows.trakt_id = fresh_fanart.media_id AND fresh_fanart.media='shows'" \
			" WHERE metadata_shows.trakt_id=? AND metadata_shows.updated_at >= " + DATE_FILTER	
		test = DB.query(SQL, [trakt_id, updated_at])
		if not test:
			meta['imdb_id'] = self.meta_map(['ids', 'imdb'], show)
			meta['tvdb_id'] = self.meta_map(['ids', 'tvdb'], show)
			meta['tmdb_id'] = self.meta_map(['ids', 'tmdb'], show)
			meta['trakt_id'] = self.meta_map(['ids', 'trakt'], show)
			meta['slug'] = self.meta_map(['ids', 'slug'], show)
			meta['title'] = self.meta_map('title', show)
			meta['TVShowTitle'] = self.meta_map('title', show)
			meta['tvshowtitle'] = self.meta_map('title', show)
			meta['rating'] = self.meta_map('rating', show)
			meta['duration'] = self.meta_map('runtime', show)
			meta['plot'] = self.meta_map('overview', show)
			meta['mpaa'] = self.meta_map('certification', show)
			meta['premiered'] = self.meta_map('first_aired', show)
			meta['year'] = int(self.meta_map('year', show, default=0))
			meta['trailer'] = self.format_trailer(self.meta_map('trailer', show))
			meta['genre'] = self.meta_map('genres', show)
			meta['studio'] = self.meta_map('network', show)
			meta['status'] = self.meta_map('status', show)
			meta['cast'] = []
			meta['banner_url'] = ''
			meta['cover_url'] = '' 
			meta['backdrop_url'] = ''
			meta['overlay'] = 6
			meta['playcount'] = 0
			if DISABLE_FANART is False and DISABLE_FANART_SERIES is False:
				if not meta['backdrop_url'] or not meta['cover_url']:
					tv_art = fanart.get_show_art(meta['tvdb_id'], meta['tmdb_id'])
					meta['backdrop_url'] = tv_art['fanart']
					meta['cover_url'] = tv_art['poster']
			
			metadata = plugin.pickle(meta)
			SQL = "REPLACE INTO metadata_shows(trakt_id, slug, imdb_id, tvdb_id, tmdb_id, updated_at, metadata) VALUES(?,?,?,?,?,?,?)"
			DB.execute(SQL, [meta['trakt_id'], meta['slug'], meta['imdb_id'], meta['tvdb_id'],  meta['tmdb_id'], updated_at, metadata])
			if DISABLE_FANART is False and DISABLE_FANART_SERIES is False and FANART_AUTOSEARCH:
				DB.execute("REPLACE INTO metadata_activities(media_id, media) VALUES(?, 'shows')", [meta['trakt_id']])
			DB.commit()
		
		else:
			meta = plugin.unpickle(str(test[0]))
			fresh = test[1] == 1 if FANART_AUTOSEARCH else 1
			if DISABLE_FANART is False and DISABLE_FANART_SERIES is False:
				if fresh is False and (not meta['backdrop_url'] or not meta['cover_url']):
					tv_art = fanart.get_show_art(meta['tvdb_id'], meta['tmdb_id'])
					meta['backdrop_url'] = tv_art['fanart']
					meta['cover_url'] = tv_art['poster']
					DB.execute("UPDATE metadata_shows SET metadata=? WHERE trakt_id=?", [trakt_id, plugin.pickle(meta)])
					DB.execute("REPLACE INTO metadata_activities(media_id, media) VALUES(?, 'shows')", [trakt_id])
					DB.commit()
		if not meta['cover_url']:
			meta['cover_url'] = SHARED_ARTWORK + "no_poster.png"
		return meta
		
	def process_movie(self, record):
		try:
			movie = record['movie']
		except:
			movie = record
		trakt_id = self.meta_map(['ids', 'trakt'], movie)
		if 'updated_at' not in movie:
			updated_at = datetime.now()
		else:
			updated_at = movie['updated_at'].replace("Z", "")
		SQL ="SELECT metadata_movies.metadata, fresh_fanart.fresh AS fresh FROM metadata_movies " \
			" LEFT JOIN fresh_fanart ON metadata_movies.trakt_id = fresh_fanart.media_id AND fresh_fanart.media='movies'" \
			" WHERE metadata_movies.trakt_id=? AND metadata_movies.updated_at >= " + DATE_FILTER	
		test = DB.query(SQL, [trakt_id, updated_at])
		if not test:
			meta = {}
			meta['imdb_id'] = self.meta_map(['ids', 'imdb'], movie)
			meta['tmdb_id'] = self.meta_map(['ids', 'tmdb'], movie)
			meta['trakt_id'] = self.meta_map(['ids', 'trakt'], movie)
			meta['slug'] = self.meta_map(['ids', 'slug'], movie)
			meta['title'] = self.meta_map('title', movie)
			meta['year'] = int(self.meta_map('year', movie, default=0))
			meta['writer'] = ''
			meta['director'] = ''
			meta['tagline'] = self.meta_map('tagline', movie)
			meta['cast'] = []
			meta['rating'] = self.meta_map('rating', movie)
			meta['votes'] = self.meta_map('votes', movie)
			meta['duration'] = self.meta_map('runtime', movie)
			meta['plot'] = self.meta_map('overview', movie)
			meta['mpaa'] = self.meta_map('certification', movie)
			meta['premiered'] = self.meta_map('released', movie)
			meta['trailer'] = self.format_trailer(self.meta_map('trailer', movie))
			meta['genre'] = self.meta_map('genres', movie)
			meta['studio'] = ''
			meta['thumb_url'] = ''
			meta['cover_url'] = ''
			meta['backdrop_url'] = ''
			meta['overlay'] = 6
			meta['playcount'] = 0
			if DISABLE_FANART is False and DISABLE_FANART_MOVIES is False:
				if not meta['backdrop_url'] or not meta['cover_url']:
					movie_art = fanart.get_movie_art(meta['imdb_id'], meta['tmdb_id'])
					meta['backdrop_url'] = movie_art['fanart']
					meta['cover_url'] = movie_art['poster']
			metadata = plugin.pickle(meta)
			SQL = "REPLACE INTO metadata_movies(trakt_id, slug, imdb_id, tmdb_id, updated_at, metadata) VALUES(?,?,?,?,?,?)"
			DB.execute(SQL, [meta['trakt_id'], meta['slug'], meta['imdb_id'], meta['tmdb_id'], updated_at, metadata])
			if DISABLE_FANART is False and DISABLE_FANART_MOVIES is False and FANART_AUTOSEARCH:
				DB.execute("REPLACE INTO metadata_activities(media_id, media) VALUES(?, 'movies')", [meta['trakt_id']])
			DB.commit()	
		else:
			meta = plugin.unpickle(str(test[0]))
			fresh = test[1] == 1 if FANART_AUTOSEARCH else 1
			if DISABLE_FANART is False and DISABLE_FANART_MOVIES is False:
				if fresh is False and (not meta['backdrop_url'] or not meta['cover_url']):
					movie_art = fanart.get_movie_art(meta['imdb_id'], meta['tmdb_id'])
					meta['backdrop_url'] = movie_art['fanart']
					meta['cover_url'] = movie_art['poster']
					DB.execute("UPDATE metadata_movies SET metadata=? WHERE trakt_id=?", [trakt_id, plugin.pickle(meta)])
					DB.execute("REPLACE INTO metadata_activities(media_id, media) VALUES(?, 'movies')", [trakt_id])
					DB.commit()
		if not meta['cover_url']:
			meta['cover_url'] = SHARED_ARTWORK + "no_poster.png"
		return meta
	
	def process_episode(self, record, watched=None, show=None):
		if 'show' in record.keys():
			show = record['show']
			episode = record['episode']
		else:
			episode = record
		trakt_id = self.meta_map(['ids', 'trakt'], episode)
		if 'updated_at' not in show:
			updated_at = datetime.now()
		else:
			updated_at = show['updated_at'].replace("Z", "")
		try:
			aired = datetime.strptime(episode['first_aired'], "%Y-%m-%dT%H:%M:%S.000Z")
		except:
			try:
				tmp = re.match('^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.000Z', episode['first_aired'])
				aired = datetime(int(tmp.group(1)), int(tmp.group(2)),int(tmp.group(3)),int(tmp.group(4)),int(tmp.group(5)),int(tmp.group(6)))
			except:
				aired = ''	
		SQL ="SELECT metadata_episodes.metadata, fresh_fanart.fresh AS fresh FROM metadata_episodes " \
			" LEFT JOIN fresh_fanart ON metadata_episodes.trakt_id = fresh_fanart.media_id AND fresh_fanart.media='episodes'" \
			" WHERE metadata_episodes.trakt_id=? AND metadata_episodes.updated_at >= " + DATE_FILTER

		test = DB.query(SQL, [trakt_id, updated_at])
		if not test:
			meta = {}
			meta['imdb_id']= self.meta_map(['ids', 'imdb'], show)
			meta['tvdb_id']= self.meta_map(['ids', 'tvdb'], show)
			meta['tmdb_id']= self.meta_map(['ids', 'tmdb'], show)
			meta['e_imdb_id']= self.meta_map(['ids', 'imdb'], episode)
			meta['e_tvdb_id']= self.meta_map(['ids', 'tvdb'], episode)
			meta['e_tmdb_id']= self.meta_map(['ids', 'tmdb'], episode)
			meta['trakt_id']= self.meta_map(['ids', 'trakt'], episode)
			meta['slug']= self.meta_map(['ids', 'slug'], show)
			meta['episode_id'] = ''
			meta['season']= int(self.meta_map('season', episode, default=0))
			meta['episode']= int(self.meta_map('number', episode, default=0))
			meta['title']= self.meta_map('title', episode)
			meta['showtitle'] = self.meta_map('title', show) 
			meta['tvshowtitle'] = self.meta_map('title', show)
			meta['director'] = ''
			meta['writer'] = ''
			meta['plot'] = self.meta_map('overview', episode)
			meta['rating'] = self.meta_map('rating', episode)
			meta['premiered'] = self.meta_map('first_aired', episode)
			meta['year'] = meta['premiered'][0:4]
			meta['poster'] = ''
			meta['cover_url']= ''
			meta['trailer_url']=''
			meta['backdrop_url'] = ''
			meta['overlay'] = 6
			meta['playcount'] = 0
			if aired:
				meta['air_date'] = aired.strftime("%m/%d/%Y")
			else:
				meta['air_date'] = ''
			if DISABLE_FANART is False and DISABLE_FANART_EPISODES is False:
				if not meta['cover_url'] or not meta['backdrop_url']:
					meta['cover_url'] = fanart.get_episode_art(meta['e_tvdb_id'], meta['tmdb_id'], meta['season'], meta['episode'], meta['tvdb_id'])
					tv_art = fanart.get_show_art(meta['tvdb_id'], meta['tmdb_id'])
					meta['backdrop_url'] = tv_art['fanart']
				
				
			metadata = plugin.pickle(meta)
			SQL = "REPLACE INTO metadata_episodes(trakt_id, updated_at, metadata) VALUES(?,?,?)"
			DB.execute(SQL, [meta['trakt_id'], updated_at, metadata])
			if DISABLE_FANART is False and DISABLE_FANART_EPISODES is False and FANART_AUTOSEARCH:
				DB.execute("REPLACE INTO metadata_activities(media_id, media) VALUES(?, 'episodes')", [meta['trakt_id']])
			DB.commit()
		else:
			meta = plugin.unpickle(str(test[0]))
			fresh = test[1] == 1 if FANART_AUTOSEARCH else 1
			if DISABLE_FANART is False and DISABLE_FANART_EPISODES is False:
				if fresh is False and (not meta['cover_url'] or not meta['backdrop_url']):
					meta['cover_url'] = fanart.get_episode_art(meta['e_tvdb_id'], meta['tmdb_id'], meta['season'], meta['episode'], meta['tvdb_id'])
					tv_art = fanart.get_show_art(meta['tvdb_id'], meta['tmdb_id'])
					meta['backdrop_url'] = tv_art['fanart']
					DB.execute("UPDATE metadata_episodes SET metadata=? WHERE trakt_id=?", [trakt_id, plugin.pickle(meta)])
					DB.execute("REPLACE INTO metadata_activities(media_id, media) VALUES(?, 'episodes')", [trakt_id])
					DB.commit()	
		if watched:
			for w in watched:
				try:
					if w['episode']['number'] == meta['episode']:
						meta['overlay'] = 7
						meta['playcount'] = 1
						break
				except:pass	
		
		if not meta['cover_url']:
			meta['cover_url'] = SHARED_ARTWORK + "no_screenshot.jpg"

		if aired:
			meta['aired'] = aired < datetime.now()
		else:
			meta['aired'] = False
		return meta


	def raise_error(self, code, title, message):
		if code in [500, 502, 503, 504, 520, 521, 522, 524]:
			message = "Temporary " + message
		image = vfs.join(ARTWORK, 'trakt_error.png')
		if vfs.exists(image) is False:
			image = vfs.join(ROOT_PATH, 'icon.png')
		plugin.error_message(title, message, image=image)

	def notify(self, title, message):
		from dudehere.routines.plugin import Plugin
		image = vfs.join(ROOT_PATH, 'icon.png')
		Plugin().error_message(title, message, image=image)
	
	def _cache_result(self, result, url, cache_limit=15):
		hash_id = hashlib.md5(url).hexdigest()
		if cache_limit > 0:
			if DB.db_type == 'mysql':
				SQL = "DELETE FROM cache WHERE unix_timestamp() - unix_timestamp(ts) > (60 * ?)"
			else:
				SQL = "DELETE FROM cache WHERE strftime('%s','now') -  strftime('%s',ts) > (60 * ?)"
			DB.execute(SQL, [cache_limit])
		SQL = "INSERT INTO cache(hash_id, url, results) VALUES(?,?,?)"
		DB.execute(SQL, [hash_id, url, json.dumps(result)])
		DB.commit()
		
	def _clear_watchlist_cache(self):
		DB.execute("DELETE FROM cache WHERE media='watchlist'")
		DB.commit()
	
	def _get_cached_result(self, url, cache_limit=15):
		result = False
		if cache_limit > 0:
			if DB.db_type == 'mysql':
				SQL = "DELETE FROM cache WHERE unix_timestamp() - unix_timestamp(ts) > (60 * ?)"
			else:
				SQL = "DELETE FROM cache WHERE strftime('%s','now') -  strftime('%s',ts) > (60 * ?)"
			DB.execute(SQL, [cache_limit])
			#DB.execute("DELETE FROM cache WHERE strftime('%s','now') -  strftime('%s',ts) > (60 * ?)", [cache_limit])
		hash_id = hashlib.md5(url).hexdigest()
		cache = DB.query("SELECT results FROM cache WHERE hash_id=?", [hash_id])
		if cache:
			result = json.loads(cache[0])
		DB.commit()
		return result

	def check_activities(self):
		results = {}
		uri = '/sync/last_activities'
		response = self._call(uri, auth=True)
		for media in ['movies', 'shows', 'seasons', 'episodes', 'lists']:
			results[media] = {}
			for activity in ['watched_at', 'watchlisted_at', 'updated_at', 'collected_at']:
				if not response: return results
				if activity in response[media]:
					ts = response[media][activity]
					if DB.db_type == 'mysql':
						SQL = "SELECT activity FROM activities WHERE activity=? AND unix_timestamp(ts) >= unix_timestamp(?)"
					else:	
						SQL = "SELECT activity FROM activities WHERE activity=? AND ts >= strftime('%s',?)"
					check = "%s_%s" % (media, activity)
					test = DB.query(SQL, [check, ts])
					if test:
						results[media][activity] = [True, ts]
					else:
						results[media][activity] = [False, ts]
		return results
	
	def check_activity(self, media, activity):
		activities = self.check_activities()
		if media in activities:
			if activity in activities[media]:
				return activities[media]
		return False
	
		
	
	def cache_request(self, fresh, activity, uri, data=None, params=None, auth=False):
		if fresh[0]:
			results = DB.query("SELECT cache FROM activity_cache WHERE activity=?", [activity])
			if results:
				plugin.log('return cached activity: %s' % activity)
				return json.loads(results[0])
		plugin.log('request remote activity: %s, %s' % (activity, uri))
		results = self._call(uri, params=params, data=data, auth=auth)
		if results:
			SQL = 'INSERT OR REPLACE ' if DB.db_type =='sqlite' else 'REPLACE '
			SQL += "INTO activity_cache(activity, cache) VALUES (?,?)"
			DB.execute(SQL, [activity, json.dumps(results)])
		if activity.endswith('_at') is False:
			activity = re.sub('_[a-zA-Z0-9]+$', '', activity)
		if DB.db_type == 'mysql':
			SQL = "REPLACE INTO activities(activity, ts) VALUES (?, unix_timestamp(?))"
		else:
			SQL = "INSERT OR REPLACE INTO activities(activity, ts) VALUES (?, strftime('%s',?))"
		DB.execute(SQL, [activity, fresh[1]])
		DB.commit()
		return results
	

	def do_authorization(self, message=None):
		a = AuthWindow("trakt_auth.xml", ROOT_PATH)
		if message is not None:
			a.message = message
		a.doModal()
		response = a.response
		del a
		return response

	def get_db_connection(self):
		return DB
	
	def generate_code(self):
		uri = '/oauth/device/code'
		data = {'client_id': CLIENT_ID}
		return self._call(uri, data=data, auth=False)

	def request_device_token(self, code):
		uri = '/oauth/device/token'
		data = {'client_id': CLIENT_ID, 'client_secret': SECRET_ID, 'code': code}
		return self._call(uri, data=data, auth=False)
	
	def refresh_token(self, refresh_token):
		uri = '/oauth/token'
		data = {'client_id': CLIENT_ID, 'client_secret': SECRET_ID, 'redirect_uri': REDIRECT_URI}
		data['refresh_token'] = refresh_token
		data['grant_type'] = 'refresh_token'
		return self._call(uri, data=data, auth=False, method='post')
	
	def _authorize(self, window=None):
		if window is not None:
			import xbmc
			response = self.generate_code()
			window.getControl(82005).setLabel("[B]%s[/B]" % response['user_code'])
			delay = 0
			delta = response['expires_in']
			while delay < response['expires_in'] and plugin.get_property('Abort') == False:
				window.getControl(82003).setLabel("Code expires in %s seconds" % delta)
				percent = int(delta / float(response['expires_in']) * 480 )
				window.getControl(82004).setWidth(percent)
				delay += 1
				delta -= 1
				if delay % response['interval'] == 0:
					token_response = self.request_device_token(response['device_code'])
					if token_response:
						plugin.set_setting('trakt_oauth_token', token_response['access_token'])
						plugin.set_setting('trakt_refresh_token', token_response['refresh_token'])
						plugin.set_setting('trakt_authorized', "true")
						self.token = token_response['access_token']
						settings = self.get_settings()
						if settings:
							plugin.set_setting('trakt_account', settings['user']['username'])
						return True
				xbmc.sleep(1000)
			return False
		else:
			refresh_token = plugin.get_setting('trakt_refresh_token')
			response = self.refresh_token(refresh_token)
			if not response: return False
			if 'access_token' in response and 'refresh_token' in response:
				plugin.set_setting('trakt_oauth_token', response['access_token'])
				plugin.set_setting('trakt_refresh_token', response['refresh_token'])
				plugin.set_setting('trakt_authorized', "true")
				self.token = response['access_token']
				return True
				
			return False
		
	def _call(self, uri, data=None, params=None, auth=False, cache_limit=False, timeout=None, quiet=False, method=None, page_limit=None, page_number=None, no_limit=False):
		if timeout is not None: self.timetout = timeout
		json_data = json.dumps(data) if data else None
		headers = {'Content-Type': 'application/json', 'trakt-api-key': CLIENT_ID, 'trakt-api-version': 2}
		url = '%s%s' % (BASE_URL, uri)
		if params:
			if 'page' in plugin.args:
				params['page'] = plugin.args['page']
			if no_limit is False:	
				params['limit'] = self.limit
			url += '?' + urllib.urlencode(params)
		elif not uri in ['/oauth/device/code', '/oauth/token', '/oauth/device/token'] or uri.startswith('/sync'):
			if no_limit is False:
				params = {'limit': self.limit}
			if 'page' in plugin.args:
				params['page'] = plugin.args['page']
			if params:
				url += '?' + urllib.urlencode(params)
		
		#plugin.log(url)
		
		if cache_limit is not False and not uri in ['/oauth/device/code', '/oauth/token', '/oauth/device/token']:
			result = self._get_cached_result(url, cache_limit)
			if result:
				response = json.loads(result)
				plugin.log("Returning cached results")
				return response
				
		if auth: 
			self.token = plugin.get_setting('trakt_oauth_token')
			headers.update({'Authorization': 'Bearer %s' % (self.token)})
			retry_attempt = False
		else:
			retry_attempt = True
		
		while True:	
			try:
				request = urllib2.Request(url, data=json_data, headers=headers)
				if method is not None:
					request.get_method = lambda: method.upper()
				f = urllib2.urlopen(request, timeout=self.timeout)
				page_count = f.info().getheader('X-Pagination-Page-Count')
				result = f.read()
				response = json.loads(result)
				
				break
			except HTTPError as e:
				if e.code in [401,405]:
					if uri in ['/oauth/device/code', '/oauth/token', '/oauth/device/token'] is False or retry_attempt:
						plugin.set_setting('trakt_oauth_token', '')
						plugin.set_setting('trakt_refresh_token', '')
						plugin.set_setting('trakt_authorized', "false")
						self.raise_error(-1, ADDON_ID, 'Trakt Authorization Required: %s' % e.code)
						raise TraktError('Trakt Authorization Required: %s' % e.code)
						return False
					else:
						self._authorize()
						second_attempt = True
				else:
					if uri in ['/oauth/device/code', '/oauth/token', '/oauth/device/token'] is False:
						plugin.log("%s: %s" % (e,url), LOG_LEVEL.VERBOSE)
						self.raise_error(e.code, "Trakt error", e)
					return False
			except URLError as e:
				plugin.log("%s: %s" % (e,url), LOG_LEVEL.VERBOSE)
				self.raise_error(e.code, "Trakt error", e)
				return False
			except socket.timeout as e:
				plugin.log("%s %s" % (e,url), LOG_LEVEL.VERBOSE)
				self.raise_error(e, "Trakt error", e)
				return False
		if cache_limit is not False:
			self._cache_result(result, url, cache_limit)
		return response

	
	def _delete(self, uri, data=None, params=None, auth=True):
		json_data = json.dumps(data) if data else None
		url = '%s%s' % (BASE_URL, uri)
		opener = urllib2.build_opener(urllib2.HTTPHandler)
		headers = {'Content-Type': 'application/json', 'trakt-api-key': CLIENT_ID, 'trakt-api-version': 2}
		if auth: 
			self.token = plugin.get_setting('trakt_oauth_token')
			headers.update({'Authorization': 'Bearer %s' % (self.token)})
			#headers.update({'Authorization': 'Bearer %s' % (self.token)})
		try:
			request = urllib2.Request(url, data=json_data, headers=headers)
			request.get_method = lambda: 'DELETE'
			response = opener.open(request)
		except HTTPError as e:
			plugin.log("%s: %s" % (e,url), LOG_LEVEL.VERBOSE)
			self.raise_error(e.code, "Trakt error", 'HTTP ERROR: %s' % e)
			return False
		except URLError as e:
			plugin.log("%s: %s" % (e,url), LOG_LEVEL.VERBOSE)
			self.raise_error(e.code, "Trakt error", 'HTTP ERROR: %s' % e)
			return False
		except socket.timeout as e:
			plugin.log("%s: %s" % (e,url), LOG_LEVEL.VERBOSE)
			self.raise_error(-1, "Trakt error", 'Socket Timeout: %s' % e)
			return False
		else:
			return response
		
