import pickle
from settings import get_setting

def load_not_wanted_magnets():
    try:
        with open('/user/db_content/not_wanted_magnets.pkl', 'rb') as f:
            return pickle.load(f)
    except (EOFError, pickle.UnpicklingError):
        # If the file is empty or not a valid pickle object, return an empty set
        return set()
    except FileNotFoundError:
        # If the file does not exist, create it and return an empty set
        with open('/user/db_content/not_wanted_magnets.pkl', 'wb') as f:
            pickle.dump(set(), f)
        return set()

def save_not_wanted_magnets(not_wanted_set):
    with open('/user/db_content/not_wanted_magnets.pkl', 'wb') as f:
        pickle.dump(not_wanted_set, f)

def add_to_not_wanted(magnet):
    not_wanted = load_not_wanted_magnets()
    not_wanted.add(magnet)
    save_not_wanted_magnets(not_wanted)

def is_magnet_not_wanted(magnet):
    if get_setting('Debug','disable_not_wanted_check', False):
        return False
    not_wanted = load_not_wanted_magnets()
    return magnet in not_wanted

def purge_not_wanted_magnets_file():
    # Purge the contents of the file by overwriting it with an empty set
    with open('/user/db_content/not_wanted_magnets.pkl', 'wb') as f:
        pickle.dump(set(), f)
    print("The 'not_wanted_magnets.pkl' file has been purged.")

# New function to get the current set of not wanted magnets
def get_not_wanted_magnets():
    return load_not_wanted_magnets()

def get_not_wanted_urls():
    return load_not_wanted_urls()

def add_to_not_wanted_urls(url):
    not_wanted = load_not_wanted_urls()
    not_wanted.add(url)
    save_not_wanted_urls(not_wanted)

def is_url_not_wanted(url):
    if get_setting('Debug','disable_not_wanted_check', False):
        return False
    not_wanted = load_not_wanted_urls()
    file_part = url.split("file=")[-1] if "file=" in url else url
    return any(file_part in nw_url.split("file=")[-1] if "file=" in nw_url else nw_url for nw_url in not_wanted)

def load_not_wanted_urls():
    try:
        with open('/user/db_content/not_wanted_urls.pkl', 'rb') as f:
            return pickle.load(f)
    except (EOFError, pickle.UnpicklingError):
        return set()
    except FileNotFoundError:
        with open('/user/db_content/not_wanted_urls.pkl', 'wb') as f:
            pickle.dump(set(), f)
        return set()
    
def save_not_wanted_urls(not_wanted_set):
    with open('/user/db_content/not_wanted_urls.pkl', 'wb') as f:
        pickle.dump(not_wanted_set, f)