import trakt.core

# Set the config path and load the config
trakt.core.CONFIG_PATH = './config/.pytrakt.json'
trakt.core.load_config()

# Get the show object for "Sunny"
show = trakt.core.get_show("Sunny")

# Get the extended show information
extended_info = show.extended_info

# Try to get the airtime from the extended info
if 'airs' in extended_info and 'time' in extended_info['airs']:
    airtime = extended_info['airs']['time']
    print(f"The airtime for Sunny is: {airtime}")
else:
    print("Could not find airtime for Sunny")
