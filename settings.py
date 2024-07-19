import configparser
import os

CONFIG_FILE = 'config.ini'

def load_config():
    config = configparser.ConfigParser()
    if os.path.exists(CONFIG_FILE):
        config.read(CONFIG_FILE)
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w') as configfile:
        config.write(configfile)

def get_setting(section, key, default=None):
    config = load_config()
    return config.get(section, key, fallback=default)

def set_setting(section, key, value):
    config = load_config()
    if not config.has_section(section):
        config.add_section(section)
    config[section][key] = value
    save_config(config)

def edit_settings():
    config = load_config()
    
    # Plex settings
    plex_url = input("Enter Plex URL (current: {}): ".format(config.get('Plex', 'url', fallback=''))) or config.get('Plex', 'url', fallback='')
    plex_token = input("Enter Plex token (current: {}): ".format(config.get('Plex', 'token', fallback=''))) or config.get('Plex', 'token', fallback='')
    
    # Overseerr settings
    overseerr_url = input("Enter Overseerr URL (current: {}): ".format(config.get('Overseerr', 'url', fallback=''))) or config.get('Overseerr', 'url', fallback='')
    overseerr_api_key = input("Enter Overseerr API key (current: {}): ".format(config.get('Overseerr', 'api_key', fallback=''))) or config.get('Overseerr', 'api_key', fallback='')
    
    # Real-Debrid settings
    real_debrid_api_key = input("Enter Real-Debrid API key (current: {}): ".format(config.get('RealDebrid', 'api_key', fallback=''))) or config.get('RealDebrid', 'api_key', fallback='')
    
    # Trakt settings
    trakt_client_id = input("Enter Trakt Client ID (current: {}): ".format(config.get('Trakt', 'client_id', fallback=''))) or config.get('Trakt', 'client_id', fallback='')
    trakt_client_secret = input("Enter Trakt Client Secret (current: {}): ".format(config.get('Trakt', 'client_secret', fallback=''))) or config.get('Trakt', 'client_secret', fallback='')
    
    # TMDB settings
    tmdb_api_key = input("Enter TMDB API key (current: {}): ".format(config.get('TMDB', 'api_key', fallback=''))) or config.get('TMDB', 'api_key', fallback='')
    
    # Zilean settings
    zilean_url = input("Enter Zilean URL (current: {}): ".format(config.get('Zilean', 'url', fallback=''))) or config.get('Zilean', 'url', fallback='')
    
    # Knightcrawler settings
    knightcrawler_url = input("Enter Knightcrawler URL (current: {}): ".format(config.get('Knightcrawler', 'url', fallback=''))) or config.get('Knightcrawler', 'url', fallback='')
    
    # mdblist settings
    mdb_api = input("Enter MDB API key (current: {}): ".format(config.get('MDBList', 'api_key', fallback=''))) or config.get('MDBList', 'api_key', fallback='')
    mdb_lists = input("Enter MDB list URLs separated by commas (current: {}): ".format(config.get('MDBList', 'urls', fallback=''))) or config.get('MDBList', 'urls', fallback='')
    
    # Save the settings
    if not config.has_section('Plex'):
        config.add_section('Plex')
    config['Plex']['url'] = plex_url
    config['Plex']['token'] = plex_token
    
    if not config.has_section('Overseerr'):
        config.add_section('Overseerr')
    config['Overseerr']['url'] = overseerr_url
    config['Overseerr']['api_key'] = overseerr_api_key
    
    if not config.has_section('RealDebrid'):
        config.add_section('RealDebrid')
    config['RealDebrid']['api_key'] = real_debrid_api_key
    
    if not config.has_section('Trakt'):
        config.add_section('Trakt')
    config['Trakt']['client_id'] = trakt_client_id
    config['Trakt']['client_secret'] = trakt_client_secret
    
    if not config.has_section('TMDB'):
        config.add_section('TMDB')
    config['TMDB']['api_key'] = tmdb_api_key
    
    if not config.has_section('Zilean'):
        config.add_section('Zilean')
    config['Zilean']['url'] = zilean_url
    
    if not config.has_section('Knightcrawler'):
        config.add_section('Knightcrawler')
    config['Knightcrawler']['url'] = knightcrawler_url
    
    if not config.has_section('MDBList'):
        config.add_section('MDBList')
    config['MDBList']['api_key'] = mdb_api
    config['MDBList']['urls'] = mdb_lists
    save_config(config)
    print("Settings saved successfully.")

if __name__ == "__main__":
    edit_settings()
