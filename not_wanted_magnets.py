import pickle

def load_not_wanted_magnets():
    try:
        with open('db_content/not_wanted_magnets.pkl', 'rb') as f:
            return pickle.load(f)
    except (EOFError, pickle.UnpicklingError):
        # If the file is empty or not a valid pickle object, return an empty set
        return set()
    except FileNotFoundError:
        # If the file does not exist, create it and return an empty set
        with open('db_content/not_wanted_magnets.pkl', 'wb') as f:
            pickle.dump(set(), f)
        return set()

def save_not_wanted_magnets(not_wanted_set):
    with open('db_content/not_wanted_magnets.pkl', 'wb') as f:
        pickle.dump(not_wanted_set, f)

def add_to_not_wanted(magnet):
    not_wanted = load_not_wanted_magnets()
    not_wanted.add(magnet)
    save_not_wanted_magnets(not_wanted)

def is_magnet_not_wanted(magnet):
    not_wanted = load_not_wanted_magnets()
    return magnet in not_wanted

def task_purge_not_wanted_magnets_file():
    # Purge the contents of the file by overwriting it with an empty set
    with open('db_content/not_wanted_magnets.pkl', 'wb') as f:
        pickle.dump(set(), f)
    print("The 'not_wanted_magnets.pkl' file has been purged.")

# New function to get the current set of not wanted magnets
def get_not_wanted_magnets():
    return load_not_wanted_magnets()