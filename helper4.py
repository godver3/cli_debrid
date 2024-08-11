from trakt.tv import TVShow

# Get the show object for "Sunny"
sunny = TVShow('Sunny')

# Get the airtime from the airs attribute
airtime = sunny.airs

print(f"The airtime for Sunny is: {airtime}")
