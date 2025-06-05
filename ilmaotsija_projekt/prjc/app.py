from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
import os
import json
from dotenv import load_dotenv
import pycountry
from datetime import datetime
import logging
import re
import unicodedata
from cachetools import TTLCache

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    FLASK_LIMITER_AVAILABLE = True
except ImportError:
    FLASK_LIMITER_AVAILABLE = False
    Limiter = None
    get_remote_address = None
    logging.warning("flask-limiter pole paigaldatud. Hindade piiramine keelatakse.")

load_dotenv()

app = Flask(__name__)
CORS(app)

# Lingid ilmakaardi toimimiseks
API_KEY = ("14f8c8233ca0bc1d78c9839a10ed8308")
GEO_BASE_URL = 'http://api.openweathermap.org/geo/1.0'
WEATHER_BASE_URL = 'https://api.openweathermap.org/data/2.5'
LOG_FILE = 'processing.log'

# Serveripoolne vahemälu: 1 tund TTL, max 1000 kirjet
search_cache = TTLCache(maxsize=1000, ttl=3600)
forecast_cache = TTLCache(maxsize=500, ttl=3600)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

if FLASK_LIMITER_AVAILABLE:
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=["200 per day", "50 per hour"]
    )
else:
    def limiter_limit(limit):
        def decorator(f):
            return f
        return decorator
    limiter = type('DummyLimiter', (), {'limit': limiter_limit})()

# Varulinnade loend juhuks, kui API ei tööta
FALLBACK_CITIES = {
    'AL': 'Tirana',
    'EE': 'Tallinn',
    'US': 'New York',
    'AS': 'Pago Pago',
    'BZ': 'Belize City',
    'BS': 'Nassau'
}

def get_country_code(country_name):
    try:
        return pycountry.countries.search_fuzzy(country_name)[0].alpha_2
    except LookupError:
        logger.warning(f"Country code not found for: {country_name}")
        return 'XX'

def get_country_name(country_code):
    if country_code == 'XX':
        return 'Unknown'
    country = pycountry.countries.get(alpha_2=country_code)
    return country.name if country else country_code

def validate_weather_data(data):
    required_fields = ['name', 'sys', 'main', 'weather', 'wind', 'coord']
    if not all(field in data for field in required_fields):
        logger.warning(f"Missing required fields: {json.dumps(data, ensure_ascii=False)}")
        return False
    if not isinstance(data['main'].get('temp'), (int, float)) or \
       not isinstance(data['main'].get('feels_like'), (int, float)) or \
       not isinstance(data['main'].get('humidity'), int) or \
       not isinstance(data['wind'].get('speed'), (int, float)) or \
       not isinstance(data['coord'].get('lat'), (int, float)) or \
       not isinstance(data['coord'].get('lon'), (int, float)):
        logger.warning(f"Invalid numeric fields: {json.dumps(data, ensure_ascii=False)}")
        return False
    if not data['weather'] or not isinstance(data['weather'], list) or not data['weather'][0].get('description'):
        logger.warning(f"Invalid weather description: {json.dumps(data, ensure_ascii=False)}")
        return False
    return True

def normalize_location(location):
    if not location or not isinstance(location, str):
        return None
    parts = location.split(',')
    if len(parts) != 2:
        logger.warning(f"Location must be in 'city,country_code' format, got: {location}")
        return None
    city, country_code = parts
    city = city.strip()
    country_code = country_code.strip().upper()
    city = re.sub(r'\blinn\b', '', city, flags=re.IGNORECASE).strip()
    city = re.sub(r'\s+', ' ', city).strip()
    city = city.replace(' ', '-')
    city = re.sub(r'-+', '-', city).strip('-')
    if not validate_country(country_code):
        logger.warning(f"Invalid country code: {country_code}")
        return None
    if not city or len(city) < 2:
        logger.warning(f"City name too short after normalization: {city}")
        return None
    normalized = f"{city},{country_code}"
    logger.info(f"Normalized location: {location} -> {normalized}")
    return normalized

def validate_location(location):
    if not location or not isinstance(location, str):
        logger.warning(f"Location is empty or not a string: {location}")
        return False
    if 'unknown city' in location.lower():
        logger.warning(f"Location contains 'unknown city': {location}")
        return False
    if not re.match(r'^[^,]+,[A-Z]{2}$', location):
        logger.warning(f"Location does not match expected format 'city,country_code': {location}")
        return False
    city, country_code = location.split(',')
    city = city.strip()
    country_code = country_code.strip()
    if len(city) < 2:
        logger.warning(f"City name too short: {city}")
        return False
    if not validate_country(country_code):
        logger.warning(f"Invalid country code: {country_code}")
        return False
    return True

def validate_country(country):
    if not country or not isinstance(country, str):
        return False
    return bool(pycountry.countries.get(alpha_2=country))

@app.route('/')
def index():
    countries = sorted([
        {"code": country.alpha_2, "name": country.name}
        for country in pycountry.countries
    ], key=lambda x: x['name'])
    logger.info(f"Serving index with {len(countries)} countries")
    return render_template('index.html', countries=countries)

@app.route('/search')
@limiter.limit("30 per minute")
def search_locations():
    query = request.args.get('q', '').strip().lower()
    country = request.args.get('country', '').strip().upper()
    page = int(request.args.get('page', 1))
    logger.info(f"Search query: q='{query}', country='{country}', page={page}")

    if not validate_country(country) and country:
        logger.warning(f"Invalid country code: {country}")
        return jsonify([]), 200

    cache_key = f"{query}|{country}|{page}"
    cached_result = search_cache.get(cache_key)
    if cached_result:
        logger.info(f"Returning cached search results for {cache_key}")
        return jsonify(cached_result)

    params = {'limit': 100, 'appid': API_KEY, 'lang': 'en'}  # Added lang=en
    if query and country:
        params['q'] = f"{query},,{country}"
    elif query:
        params['q'] = query
    elif country:
        params['q'] = f",,{country}"
    else:
        return jsonify([]), 200

    try:
        response = requests.get(f"{GEO_BASE_URL}/direct", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        seen = set()
        results = []
        for loc in data:
            if not loc.get('name') or not loc.get('country'):
                continue
            country_code = loc['country']
            if len(country_code) > 2:
                try:
                    country_code = pycountry.countries.search_fuzzy(country_code)[0].alpha_2
                except LookupError:
                    continue
            key = (loc['name'].lower(), country_code, loc.get('lat'), loc.get('lon'))
            if key in seen:
                continue
            seen.add(key)
            result = {
                'name': loc['name'],
                'country': country_code,
                'lat': loc.get('lat'),
                'lon': loc.get('lon')
            }
            if loc.get('state'):
                result['state'] = loc['state']
            results.append(result)

        results.sort(key=lambda x: x['name'])
        start = (page - 1) * 100
        end = start + 100
        paginated_results = results[start:end]

        if not paginated_results and not query and country in FALLBACK_CITIES:
            logger.info(f"No results for country {country}, trying fallback city: {FALLBACK_CITIES[country]}")
            params['q'] = f"{FALLBACK_CITIES[country]},,{country}"
            response = requests.get(f"{GEO_BASE_URL}/direct", params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            for loc in data:
                if not loc.get('name') or not loc.get('country'):
                    continue
                country_code = loc['country']
                if len(country_code) > 2:
                    try:
                        country_code = pycountry.countries.search_fuzzy(country_code)[0].alpha_2
                    except LookupError:
                        continue
                key = (loc['name'].lower(), country_code, loc.get('lat'), loc.get('lon'))
                if key not in seen:
                    seen.add(key)
                    result = {
                        'name': loc['name'],
                        'country': country_code,
                        'lat': loc.get('lat'),
                        'lon': loc.get('lon')
                    }
                    if loc.get('state'):
                        result['state'] = loc['state']
                    paginated_results.append(result)

        paginated_results.sort(key=lambda x: x['name'])
        search_cache[cache_key] = paginated_results
        logger.info(f"Search returned {len(paginated_results)} unique cities for page {page}")
        return jsonify(paginated_results)

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.warning(f"No cities found for query: q='{query}', country='{country}', page={page}")
            if not query and country in FALLBACK_CITIES:
                logger.info(f"Retrying with fallback city: {FALLBACK_CITIES[country]}")
                try:
                    params['q'] = f"{FALLBACK_CITIES[country]},,{country}"
                    response = requests.get(f"{GEO_BASE_URL}/direct", params=params, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    seen = set()
                    results = []
                    for loc in data:
                        if not loc.get('name') or not loc.get('country'):
                            continue
                        country_code = loc['country']
                        if len(country_code) > 2:
                            try:
                                country_code = pycountry.countries.search_fuzzy(country_code)[0].alpha_2
                            except LookupError:
                                continue
                        key = (loc['name'].lower(), country_code, loc.get('lat'), loc.get('lon'))
                        if key not in seen:
                            seen.add(key)
                            result = {
                                'name': loc['name'],
                                'country': country_code,
                                'lat': loc.get('lat'),
                                'lon': loc.get('lon')
                            }
                            if loc.get('state'):
                                result['state'] = loc['state']
                            results.append(result)
                    results.sort(key=lambda x: x['name'])
                    search_cache[cache_key] = results
                    logger.info(f"Fallback search returned {len(results)} unique cities")
                    return jsonify(results)
                except requests.exceptions.HTTPError as e2:
                    logger.warning(f"Fallback search failed: {str(e2)}")
                    return jsonify([]), 200
            return jsonify([]), 200
        logger.error(f"Geocoding API error: {str(e)}")
        return jsonify({'error': str(e)}), 500
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error: {str(e)}")
        return jsonify({'error': 'Network error'}), 500
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({'error': 'Unexpected error'}), 500

@app.route('/forecast')
@limiter.limit("30 per minute")
def get_forecast():
    location = request.args.get('location')
    if not location:
        logger.warning("Location parameter missing")
        return jsonify({'error': 'Location parameter required'}), 400

    if not validate_location(location):
        logger.warning(f"Invalid location format: {location}")
        return jsonify({'error': f'Invalid location format: "{location}". Expected format: "city,country_code" (e.g., "Tallinn,EE"). City may contain letters, spaces, hyphens, and special characters.'}), 400

    normalized_location = normalize_location(location)
    if not normalized_location:
        logger.warning(f"Failed to normalize location: {location}")
        return jsonify({'error': f'Invalid location after normalization: "{location}". Ensure city name is valid and country code exists.'}), 400

    cache_key = f"forecast_{normalized_location}"
    cached_result = forecast_cache.get(cache_key)
    if cached_result:
        logger.info(f"Returning cached forecast for {cache_key}")
        return jsonify(cached_result)

    try:
        geo_url = f"{GEO_BASE_URL}/direct?q={normalized_location}&limit=1&appid={API_KEY}&lang=en"  # Added lang=en
        logger.info(f"Geocoding request: {geo_url}")
        geo_response = requests.get(geo_url, timeout=10)
        geo_response.raise_for_status()
        geo_data = geo_response.json()

        if not geo_data:
            logger.warning(f"Location not found in geocoding: {normalized_location}")
            return jsonify({'error': f'Location "{location}" not found'}), 404

        lat = geo_data[0]['lat']
        lon = geo_data[0]['lon']
        city = geo_data[0]['name']
        country_code = geo_data[0].get('country', 'XX')

        url = f"{WEATHER_BASE_URL}/forecast?lat={lat}&lon={lon}&appid={API_KEY}&units=metric&lang=en"  # Added lang=en
        logger.info(f"Forecast request: {url}")
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if 'list' not in data:
            logger.error(f"Invalid forecast data for '{normalized_location}'")
            return jsonify({'error': f'Invalid forecast data for "{location}"'}), 500

        formatted = {
            'city': city,
            'country': get_country_name(country_code),
            'forecast': []
        }

        daily_data = {}
        for entry in data['list']:
            dt = datetime.fromtimestamp(entry['dt'])
            date = dt.strftime('%Y-%m-%d')
            if date not in daily_data:
                daily_data[date] = {
                    'temps': [],
                    'descriptions': [],
                    'icons': []
                }
            daily_data[date]['temps'].append(entry['main']['temp'])
            daily_data[date]['descriptions'].append(entry['weather'][0]['description'])
            daily_data[date]['icons'].append(entry['weather'][0]['icon'])

        for date, info in daily_data.items():
            avg_temp = sum(info['temps']) / len(info['temps'])
            most_common_desc = max(set(info['descriptions']), key=info['descriptions'].count)
            most_common_icon = max(set(info['icons']), key=info['icons'].count)
            formatted['forecast'].append({
                'date': date,
                'temp': round(avg_temp, 1),
                'description': most_common_desc,
                'icon': most_common_icon
            })

        forecast_cache[cache_key] = formatted
        logger.info(f"Returning forecast for {location}")
        return jsonify(formatted)

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            logger.error("Weather API error: Invalid API key")
            return jsonify({'error': 'Invalid API key'}), 401
        if e.response.status_code == 429:
            logger.error("Weather API error: Rate limit exceeded")
            return jsonify({'error': 'Rate limit exceeded'}), 429
        if e.response.status_code == 404:
            logger.warning(f"Location not found: {normalized_location}")
            return jsonify({'error': f'Location "{location}" not found'}), 404
        logger.error(f"Weather API error: {str(e)}")
        return jsonify({'error': f'Weather service error: {str(e)}'}), 500
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error: {str(e)}")
        return jsonify({'error': f'Network error: {str(e)}'}), 500
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

@app.route('/countries')
def get_countries():
    countries = sorted([
        {"code": country.alpha_2, "name": country.name}
        for country in pycountry.countries
    ], key=lambda x: x['name'])
    logger.info(f"Returning {len(countries)} countries")
    return jsonify(countries)

@app.route('/weather')
@limiter.limit("30 per minute")
def get_weather():
    location = request.args.get('location')
    if not location:
        logger.warning("Location parameter missing")
        return jsonify({'error': 'Location parameter required'}), 400

    if not validate_location(location):
        logger.warning(f"Invalid location format: {location}")
        return jsonify({'error': f'Invalid location format: "{location}". Expected format: "city,country_code" (e.g., "Tallinn,EE"). City may contain letters, spaces, hyphens, and special characters.'}), 400

    normalized_location = normalize_location(location)
    if not normalized_location:
        logger.warning(f"Failed to normalize location: {location}")
        return jsonify({'error': f'Invalid location after normalization: "{location}". Ensure city name is valid and country code exists.'}), 400

    try:
        geo_url = f"{GEO_BASE_URL}/direct?q={normalized_location}&limit=1&appid={API_KEY}&lang=en"  # Added lang=en
        logger.info(f"Geocoding request: {geo_url}")
        geo_response = requests.get(geo_url, timeout=10)
        geo_response.raise_for_status()
        geo_data = geo_response.json()

        if not geo_data:
            logger.warning(f"Location not found in geocoding: {normalized_location}")
            return jsonify({'error': f'Location "{location}" not found'}), 404

        lat = geo_data[0]['lat']
        lon = geo_data[0]['lon']
        city = geo_data[0]['name']
        country_code = geo_data[0].get('country', 'XX')

        url = f"{WEATHER_BASE_URL}/weather?lat={lat}&lon={lon}&appid={API_KEY}&units=metric&lang=en"  # Added lang=en
        logger.info(f"Weather request: {url}")
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if not validate_weather_data(data):
            logger.error(f"Invalid weather data for '{normalized_location}'")
            return jsonify({'error': f'Invalid weather data for "{location}"'}), 500

        formatted = {
            'city': city,
            'country': get_country_name(country_code),
            'temp': data['main']['temp'],
            'feels_like': data['main']['feels_like'],
            'weather': data['weather'][0]['main'],
            'description': data['weather'][0]['description'],
            'humidity': data['main']['humidity'],
            'wind_speed': data['wind']['speed'],
            'icon': data['weather'][0]['icon'],
            'lat': data['coord']['lat'],
            'lon': data['coord']['lon']
        }
        logger.info(f"Returning weather for {location}")
        return jsonify(formatted)

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            logger.error("Weather API error: Invalid API key")
            return jsonify({'error': 'Invalid API key'}), 401
        if e.response.status_code == 429:
            logger.error("Weather API error: Rate limit exceeded")
            return jsonify({'error': 'Rate limit exceeded'}), 429
        if e.response.status_code == 404:
            logger.warning(f"Location not found: {normalized_location}")
            return jsonify({'error': f'Location "{location}" not found'}), 404
        logger.error(f"Weather API error: {str(e)}")
        return jsonify({'error': f'Weather service error: {str(e)}'}), 500
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error: {str(e)}")
        return jsonify({'error': f'Network error: {str(e)}'}), 500
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/x-icon')

@app.route('/static/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

if __name__ == '__main__':
    app.run(debug=True, port=5001)