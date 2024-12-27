from guessit import guessit
from PTT import parse

test_titles = [
    "laid.us.s01e07.1080p.web.h264-successfulcrab",
    "This.Is.Us.S06E18.1080p.WEB.H264-CAKES",
    "Made.in.the.UK.S01E01.1080p.WEB.H264",
    "Show.UK.S01E01.720p.WEB.x264",
    "Single.US.2024.1080p.WEB.H264"
]

print("\nTesting title parsing with both parsers:\n")

for title in test_titles:
    print(f"\nTesting title: {title}")
    
    # Test guessit
    try:
        guessit_result = guessit(title)
        print(f"Guessit result: {guessit_result}")
        guessit_title = guessit_result.get('title', '')
        print(f"Guessit parsed title: {guessit_title}")
    except Exception as e:
        print(f"Guessit error: {str(e)}")
        guessit_title = None
        
    # Test PTT
    try:
        ptt_result = parse(title)
        print(f"PTT result: {ptt_result}")
        ptt_title = ptt_result.get('title', '')
        print(f"PTT parsed title: {ptt_title}")
    except Exception as e:
        print(f"PTT error: {str(e)}")
        ptt_title = None
