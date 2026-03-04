"""
font_manager.py — Google Fonts download and cache for PIL rendering.

Fonts are downloaded from Google Fonts on first use and cached locally in
overlays/fonts/cache/.  Subsequent renders use the cached TTF file.

Usage:
    from overlays.font_manager import get_pil_font
    font = get_pil_font('Bebas Neue', 24, bold=False)
"""

import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Cache directory: prefer the persistent user volume so fonts survive container
# restarts/updates.  Fall back to the local overlays/fonts/cache/ for dev envs.
_USER_FONT_CACHE = Path(os.environ.get('USER_DIR', '/user')) / 'config' / 'overlay_fonts_cache'
_LOCAL_FONT_CACHE = Path(__file__).parent / 'fonts' / 'cache'
FONT_CACHE_DIR = _USER_FONT_CACHE if _USER_FONT_CACHE.parent.exists() else _LOCAL_FONT_CACHE

# Use IE 8 User-Agent: Google Fonts v1 API originally returned TTF for this UA,
# but Google now returns WOFF2 regardless of UA.  Kept for potential future use.
_UA = 'Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 6.0)'

# Google Fonts v1 CSS endpoint
_GFONTS_CSS_V1 = 'https://fonts.googleapis.com/css?family={family}:{variant}'

# Direct TTF URLs from the official google/fonts GitHub repository.
# Used as primary download source since the CSS API now returns WOFF2 for all UAs.
# Keys are (family, style) where style is 'Regular'/'Bold'/'Italic'/'BoldItalic'.
# Only Regular/Bold are listed for most fonts; italic falls back to Regular when unavailable.
_GF = 'https://raw.githubusercontent.com/google/fonts/main'
_DIRECT_FONT_URLS: dict[str, dict[str, str]] = {
    'Anton':                {'Regular': f'{_GF}/ofl/anton/Anton-Regular.ttf'},
    'Archivo Black':        {'Regular': f'{_GF}/ofl/archivoblack/ArchivoBlack-Regular.ttf'},
    'Arimo':                {'Regular': f'{_GF}/apache/arimo/Arimo%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/apache/arimo/Arimo%5Bwght%5D.ttf'},
    'Barlow':               {'Regular': f'{_GF}/ofl/barlow/Barlow-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/barlow/Barlow-Bold.ttf',
                             'Italic':  f'{_GF}/ofl/barlow/Barlow-Italic.ttf',
                             'BoldItalic': f'{_GF}/ofl/barlow/Barlow-BoldItalic.ttf'},
    'Barlow Condensed':     {'Regular':    f'{_GF}/ofl/barlowcondensed/BarlowCondensed-Regular.ttf',
                             'Bold':       f'{_GF}/ofl/barlowcondensed/BarlowCondensed-Bold.ttf',
                             'Italic':     f'{_GF}/ofl/barlowcondensed/BarlowCondensed-Italic.ttf',
                             'BoldItalic': f'{_GF}/ofl/barlowcondensed/BarlowCondensed-BoldItalic.ttf'},
    'Barlow Semi Condensed':{'Regular':    f'{_GF}/ofl/barlowsemicondensed/BarlowSemiCondensed-Regular.ttf',
                             'Bold':       f'{_GF}/ofl/barlowsemicondensed/BarlowSemiCondensed-Bold.ttf'},
    'Bebas Neue':           {'Regular':    f'{_GF}/ofl/bebasneue/BebasNeue-Regular.ttf'},
    'Black Han Sans':       {'Regular': f'{_GF}/ofl/blackhansans/BlackHanSans-Regular.ttf'},
    'Black Ops One':        {'Regular': f'{_GF}/ofl/blackopsone/BlackOpsOne-Regular.ttf'},
    'Boogaloo':             {'Regular': f'{_GF}/ofl/boogaloo/Boogaloo-Regular.ttf'},
    'Cabin':                {'Regular': f'{_GF}/ofl/cabin/Cabin%5Bwdth%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/cabin/Cabin%5Bwdth%2Cwght%5D.ttf'},
    'Chakra Petch':         {'Regular': f'{_GF}/ofl/chakrapetch/ChakraPetch-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/chakrapetch/ChakraPetch-Bold.ttf',
                             'Italic':  f'{_GF}/ofl/chakrapetch/ChakraPetch-Italic.ttf',
                             'BoldItalic': f'{_GF}/ofl/chakrapetch/ChakraPetch-BoldItalic.ttf'},
    'Cinzel':               {'Regular': f'{_GF}/ofl/cinzel/Cinzel%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/cinzel/Cinzel%5Bwght%5D.ttf'},
    'Courier Prime':        {'Regular': f'{_GF}/ofl/courierprime/CourierPrime-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/courierprime/CourierPrime-Bold.ttf',
                             'Italic':  f'{_GF}/ofl/courierprime/CourierPrime-Italic.ttf',
                             'BoldItalic': f'{_GF}/ofl/courierprime/CourierPrime-BoldItalic.ttf'},
    'Crimson Text':         {'Regular': f'{_GF}/ofl/crimsontext/CrimsonText-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/crimsontext/CrimsonText-Bold.ttf',
                             'Italic':  f'{_GF}/ofl/crimsontext/CrimsonText-Italic.ttf',
                             'BoldItalic': f'{_GF}/ofl/crimsontext/CrimsonText-BoldItalic.ttf'},
    'DM Mono':              {'Regular': f'{_GF}/ofl/dmmono/DMMono-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/dmmono/DMMono-Medium.ttf',
                             'Italic':  f'{_GF}/ofl/dmmono/DMMono-Italic.ttf'},
    'DM Sans':              {'Regular': f'{_GF}/ofl/dmsans/DMSans%5Bopsz%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/dmsans/DMSans%5Bopsz%2Cwght%5D.ttf'},
    'DM Serif Display':     {'Regular': f'{_GF}/ofl/dmseriftext/DMSerifText-Regular.ttf',
                             'Italic':  f'{_GF}/ofl/dmseriftext/DMSerifText-Italic.ttf'},
    'EB Garamond':          {'Regular': f'{_GF}/ofl/ebgaramond/EBGaramond%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/ebgaramond/EBGaramond%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/ebgaramond/EBGaramond-Italic%5Bwght%5D.ttf'},
    'Exo':                  {'Regular': f'{_GF}/ofl/exo/Exo%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/exo/Exo%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/exo/Exo-Italic%5Bwght%5D.ttf'},
    'Exo 2':                {'Regular': f'{_GF}/ofl/exo2/Exo2%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/exo2/Exo2%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/exo2/Exo2-Italic%5Bwght%5D.ttf'},
    'Figtree':              {'Regular': f'{_GF}/ofl/figtree/Figtree%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/figtree/Figtree%5Bwght%5D.ttf'},
    'Fira Code':            {'Regular': f'{_GF}/ofl/firacode/FiraCode%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/firacode/FiraCode%5Bwght%5D.ttf'},
    'Fjalla One':           {'Regular': f'{_GF}/ofl/fjallaone/FjallaOne-Regular.ttf'},
    'Francois One':         {'Regular': f'{_GF}/ofl/francoisone/FrancoisOne-Regular.ttf'},
    'Fredoka':              {'Regular': f'{_GF}/ofl/fredoka/Fredoka%5Bwdth%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/fredoka/Fredoka%5Bwdth%2Cwght%5D.ttf'},
    'Fraunces':             {'Regular': f'{_GF}/ofl/fraunces/Fraunces%5BSOFT%2CWONK%2Copsz%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/fraunces/Fraunces%5BSOFT%2CWONK%2Copsz%2Cwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/fraunces/Fraunces-Italic%5BSOFT%2CWONK%2Copsz%2Cwght%5D.ttf'},
    'Graduate':             {'Regular': f'{_GF}/ofl/graduate/Graduate-Regular.ttf'},
    'Grandstander':         {'Regular': f'{_GF}/ofl/grandstander/Grandstander%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/grandstander/Grandstander%5Bwght%5D.ttf'},
    'Hind':                 {'Regular': f'{_GF}/ofl/hind/Hind-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/hind/Hind-Bold.ttf'},
    'IBM Plex Mono':        {'Regular': f'{_GF}/ofl/ibmplexmono/IBMPlexMono-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/ibmplexmono/IBMPlexMono-Bold.ttf',
                             'Italic':  f'{_GF}/ofl/ibmplexmono/IBMPlexMono-Italic.ttf',
                             'BoldItalic': f'{_GF}/ofl/ibmplexmono/IBMPlexMono-BoldItalic.ttf'},
    'IBM Plex Sans':        {'Regular': f'{_GF}/ofl/ibmplexsans/IBMPlexSans%5Bwdth%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/ibmplexsans/IBMPlexSans%5Bwdth%2Cwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/ibmplexsans/IBMPlexSans-Italic%5Bwdth%2Cwght%5D.ttf'},
    'Inconsolata':          {'Regular': f'{_GF}/ofl/inconsolata/Inconsolata%5Bwdth%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/inconsolata/Inconsolata%5Bwdth%2Cwght%5D.ttf'},
    'Inter':                {'Regular': f'{_GF}/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf'},
    'JetBrains Mono':       {'Regular': f'{_GF}/ofl/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/jetbrainsmono/JetBrainsMono-Italic%5Bwght%5D.ttf'},
    'Jockey One':           {'Regular': f'{_GF}/ofl/jockeyone/JockeyOne-Regular.ttf'},
    'Josefin Sans':         {'Regular': f'{_GF}/ofl/josefinsans/JosefinSans%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/josefinsans/JosefinSans%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/josefinsans/JosefinSans-Italic%5Bwght%5D.ttf'},
    'Kanit':                {'Regular': f'{_GF}/ofl/kanit/Kanit-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/kanit/Kanit-Bold.ttf',
                             'Italic':  f'{_GF}/ofl/kanit/Kanit-Italic.ttf',
                             'BoldItalic': f'{_GF}/ofl/kanit/Kanit-BoldItalic.ttf'},
    'Karla':                {'Regular': f'{_GF}/ofl/karla/Karla%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/karla/Karla%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/karla/Karla-Italic%5Bwght%5D.ttf'},
    'Kavoon':               {'Regular': f'{_GF}/ofl/kavoon/Kavoon-Regular.ttf'},
    'Lalezar':              {'Regular': f'{_GF}/ofl/lalezar/Lalezar-Regular.ttf'},
    'Lato':                 {'Regular': f'{_GF}/ofl/lato/Lato-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/lato/Lato-Bold.ttf',
                             'Italic':  f'{_GF}/ofl/lato/Lato-Italic.ttf',
                             'BoldItalic': f'{_GF}/ofl/lato/Lato-BoldItalic.ttf'},
    'Libre Baskerville':    {'Regular': f'{_GF}/ofl/librebaskerville/LibreBaskerville%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/librebaskerville/LibreBaskerville%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/librebaskerville/LibreBaskerville-Italic%5Bwght%5D.ttf'},
    'Lobster':              {'Regular': f'{_GF}/ofl/lobster/Lobster-Regular.ttf'},
    'Lora':                 {'Regular': f'{_GF}/ofl/lora/Lora%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/lora/Lora%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/lora/Lora-Italic%5Bwght%5D.ttf'},
    'Manrope':              {'Regular': f'{_GF}/ofl/manrope/Manrope%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/manrope/Manrope%5Bwght%5D.ttf'},
    'Merriweather':         {'Regular': f'{_GF}/ofl/merriweather/Merriweather%5Bopsz%2Cwdth%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/merriweather/Merriweather%5Bopsz%2Cwdth%2Cwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/merriweather/Merriweather-Italic%5Bopsz%2Cwdth%2Cwght%5D.ttf'},
    'Monoton':              {'Regular': f'{_GF}/ofl/monoton/Monoton-Regular.ttf'},
    'Montserrat':           {'Regular': f'{_GF}/ofl/montserrat/Montserrat%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/montserrat/Montserrat%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/montserrat/Montserrat-Italic%5Bwght%5D.ttf'},
    'Mulish':               {'Regular': f'{_GF}/ofl/mulish/Mulish%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/mulish/Mulish%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/mulish/Mulish-Italic%5Bwght%5D.ttf'},
    'Noto Sans':            {'Regular': f'{_GF}/ofl/notosans/NotoSans%5Bwdth%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/notosans/NotoSans%5Bwdth%2Cwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/notosans/NotoSans-Italic%5Bwdth%2Cwght%5D.ttf'},
    'Nunito':               {'Regular': f'{_GF}/ofl/nunito/Nunito%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/nunito/Nunito%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/nunito/Nunito-Italic%5Bwght%5D.ttf'},
    'Open Sans':            {'Regular': f'{_GF}/ofl/opensans/OpenSans%5Bwdth%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/opensans/OpenSans%5Bwdth%2Cwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/opensans/OpenSans-Italic%5Bwdth%2Cwght%5D.ttf'},
    'Orbitron':             {'Regular': f'{_GF}/ofl/orbitron/Orbitron%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/orbitron/Orbitron%5Bwght%5D.ttf'},
    'Oswald':               {'Regular': f'{_GF}/ofl/oswald/Oswald%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/oswald/Oswald%5Bwght%5D.ttf'},
    'Outfit':               {'Regular': f'{_GF}/ofl/outfit/Outfit%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/outfit/Outfit%5Bwght%5D.ttf'},
    'Overpass':             {'Regular': f'{_GF}/ofl/overpass/Overpass%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/overpass/Overpass%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/overpass/Overpass-Italic%5Bwght%5D.ttf'},
    'Oxanium':              {'Regular': f'{_GF}/ofl/oxanium/Oxanium%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/oxanium/Oxanium%5Bwght%5D.ttf'},
    'Oxygen Mono':          {'Regular': f'{_GF}/ofl/oxygenmono/OxygenMono-Regular.ttf'},
    'Permanent Marker':     {'Regular': f'{_GF}/apache/permanentmarker/PermanentMarker-Regular.ttf'},
    'Playfair Display':     {'Regular': f'{_GF}/ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/playfairdisplay/PlayfairDisplay-Italic%5Bwght%5D.ttf'},
    'Poller One':           {'Regular': f'{_GF}/ofl/pollerone/PollerOne.ttf'},
    'Poppins':              {'Regular': f'{_GF}/ofl/poppins/Poppins-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/poppins/Poppins-Bold.ttf',
                             'Italic':  f'{_GF}/ofl/poppins/Poppins-Italic.ttf',
                             'BoldItalic': f'{_GF}/ofl/poppins/Poppins-BoldItalic.ttf'},
    'Press Start 2P':       {'Regular': f'{_GF}/ofl/pressstart2p/PressStart2P-Regular.ttf'},
    'Public Sans':          {'Regular': f'{_GF}/ofl/publicsans/PublicSans%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/publicsans/PublicSans%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/publicsans/PublicSans-Italic%5Bwght%5D.ttf'},
    'Questrial':            {'Regular': f'{_GF}/ofl/questrial/Questrial-Regular.ttf'},
    'Quicksand':            {'Regular': f'{_GF}/ofl/quicksand/Quicksand%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/quicksand/Quicksand%5Bwght%5D.ttf'},
    'Rajdhani':             {'Regular': f'{_GF}/ofl/rajdhani/Rajdhani-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/rajdhani/Rajdhani-Bold.ttf'},
    'Readex Pro':           {'Regular': f'{_GF}/ofl/readexpro/ReadexPro%5BHEXP%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/readexpro/ReadexPro%5BHEXP%2Cwght%5D.ttf'},
    'Righteous':            {'Regular': f'{_GF}/ofl/righteous/Righteous-Regular.ttf'},
    'Roboto':               {'Regular': f'{_GF}/ofl/roboto/Roboto%5Bwdth%2Cwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/roboto/Roboto%5Bwdth%2Cwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/roboto/Roboto-Italic%5Bwdth%2Cwght%5D.ttf'},
    'Roboto Condensed':     {'Regular': f'{_GF}/ofl/robotocondensed/RobotoCondensed%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/robotocondensed/RobotoCondensed%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/robotocondensed/RobotoCondensed-Italic%5Bwght%5D.ttf'},
    'Roboto Mono':          {'Regular': f'{_GF}/ofl/robotomono/RobotoMono%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/robotomono/RobotoMono%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/robotomono/RobotoMono-Italic%5Bwght%5D.ttf'},
    'Rubik':                {'Regular': f'{_GF}/ofl/rubik/Rubik%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/rubik/Rubik%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/rubik/Rubik-Italic%5Bwght%5D.ttf'},
    'Russo One':            {'Regular': f'{_GF}/ofl/russoone/RussoOne-Regular.ttf'},
    'Saira Condensed':      {'Regular': f'{_GF}/ofl/sairacondensed/SairaCondensed-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/sairacondensed/SairaCondensed-Bold.ttf'},
    'Share Tech Mono':      {'Regular': f'{_GF}/ofl/sharetechmono/ShareTechMono-Regular.ttf'},
    'Sigmar One':           {'Regular': f'{_GF}/ofl/sigmarone/SigmarOne-Regular.ttf'},
    'Silkscreen':           {'Regular': f'{_GF}/ofl/silkscreen/Silkscreen-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/silkscreen/Silkscreen-Bold.ttf'},
    'Sora':                 {'Regular': f'{_GF}/ofl/sora/Sora%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/sora/Sora%5Bwght%5D.ttf'},
    'Source Code Pro':      {'Regular': f'{_GF}/ofl/sourcecodepro/SourceCodePro%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/sourcecodepro/SourceCodePro%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/sourcecodepro/SourceCodePro-Italic%5Bwght%5D.ttf'},
    'Space Grotesk':        {'Regular': f'{_GF}/ofl/spacegrotesk/SpaceGrotesk%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/spacegrotesk/SpaceGrotesk%5Bwght%5D.ttf'},
    'Space Mono':           {'Regular': f'{_GF}/ofl/spacemono/SpaceMono-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/spacemono/SpaceMono-Bold.ttf',
                             'Italic':  f'{_GF}/ofl/spacemono/SpaceMono-Italic.ttf',
                             'BoldItalic': f'{_GF}/ofl/spacemono/SpaceMono-BoldItalic.ttf'},
    'Spectral':             {'Regular': f'{_GF}/ofl/spectral/Spectral-Regular.ttf',
                             'Bold':    f'{_GF}/ofl/spectral/Spectral-Bold.ttf',
                             'Italic':  f'{_GF}/ofl/spectral/Spectral-Italic.ttf',
                             'BoldItalic': f'{_GF}/ofl/spectral/Spectral-BoldItalic.ttf'},
    'Squada One':           {'Regular': f'{_GF}/ofl/squadaone/SquadaOne-Regular.ttf'},
    'Teko':                 {'Regular': f'{_GF}/ofl/teko/Teko%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/teko/Teko%5Bwght%5D.ttf'},
    'Titan One':            {'Regular': f'{_GF}/ofl/titanone/TitanOne-Regular.ttf'},
    'Ubuntu':               {'Regular':    f'{_GF}/ufl/ubuntu/Ubuntu-Regular.ttf',
                             'Bold':       f'{_GF}/ufl/ubuntu/Ubuntu-Bold.ttf',
                             'Italic':     f'{_GF}/ufl/ubuntu/Ubuntu-Italic.ttf',
                             'BoldItalic': f'{_GF}/ufl/ubuntu/Ubuntu-BoldItalic.ttf'},
    'Ultra':                {'Regular': f'{_GF}/apache/ultra/Ultra-Regular.ttf'},
    'Urbanist':             {'Regular': f'{_GF}/ofl/urbanist/Urbanist%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/urbanist/Urbanist%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/urbanist/Urbanist-Italic%5Bwght%5D.ttf'},
    'Wallpoet':             {'Regular': f'{_GF}/ofl/wallpoet/Wallpoet-Regular.ttf'},
    'Work Sans':            {'Regular': f'{_GF}/ofl/worksans/WorkSans%5Bwght%5D.ttf',
                             'Bold':    f'{_GF}/ofl/worksans/WorkSans%5Bwght%5D.ttf',
                             'Italic':  f'{_GF}/ofl/worksans/WorkSans-Italic%5Bwght%5D.ttf'},
}

# System font fallback paths (tried in order when all downloads fail)
_SYSTEM_FALLBACKS = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
]

# Local font names that map directly to system TTF paths — never hit Google Fonts for these.
_LOCAL_FONT_MAP: dict[str, str] = {
    'DejaVuSans-Bold':    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    'DejaVuSans':         '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    'DejaVuSans-Oblique': '/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf',
    'LiberationSans-Bold':    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    'LiberationSans-Regular': '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
}

# Magic bytes that identify a file as a valid TTF/OTF font PIL can load.
# WOFF  = b'wOFF', WOFF2 = b'wOF2' — both rejected by Pillow.
_VALID_FONT_MAGIC = {
    b'\x00\x01\x00\x00',  # TrueType (most TTFs)
    b'true',               # TrueType (Mac variant)
    b'OTTO',               # OpenType CFF
    b'typ1',               # Type 1 OT wrapper
}


def _safe_family_name(family: str) -> str:
    """Convert a font family name to a safe filename prefix."""
    return re.sub(r'[^\w]', '_', family).strip('_')


def _is_valid_font_file(path: Path) -> bool:
    """Return True if path exists and starts with a known TTF/OTF magic header."""
    try:
        with open(path, 'rb') as f:
            header = f.read(4)
        return header in _VALID_FONT_MAGIC
    except Exception:
        return False


def get_font_path(family: str, bold: bool = False, italic: bool = False) -> str | None:
    """
    Return local path to a Google Font TTF, downloading it if necessary.

    Args:
        family: Google Font family name, e.g. 'Bebas Neue'
        bold:   True to request the 700-weight variant
        italic: True to request the italic variant

    Returns:
        Absolute path string, or None if the font could not be obtained.
    """
    # Local system fonts — resolve directly, never attempt a Google Fonts download.
    if family in _LOCAL_FONT_MAP:
        path = _LOCAL_FONT_MAP[family]
        if os.path.exists(path):
            return path
        # Path not found on this system — fall through to normal lookup
        logger.debug(f"Local font '{family}' not found at {path}, trying Google Fonts")

    FONT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if bold and italic:
        style = 'BoldItalic'
    elif bold:
        style = 'Bold'
    elif italic:
        style = 'Italic'
    else:
        style = 'Regular'
    cache_path = FONT_CACHE_DIR / f'{_safe_family_name(family)}-{style}.ttf'

    if cache_path.exists():
        if _is_valid_font_file(cache_path):
            return str(cache_path)
        # Cached file is corrupt (e.g. WOFF2 saved with .ttf extension) — delete and re-fetch
        logger.warning(f"Cached font '{cache_path.name}' is not a valid TTF — deleting and re-downloading")
        try:
            cache_path.unlink()
        except Exception:
            pass

    logger.info(f"Font cache miss — downloading '{family}' ({style})")
    # Try direct GitHub TTF URL first (more reliable than CSS API which now returns WOFF2)
    path = _download_direct(family, style, cache_path)
    if path:
        return path
    # Fall back to Google Fonts CSS API
    return _download_google_font(family, bold, italic, cache_path)


def _download_direct(family: str, style: str, dest: Path) -> str | None:
    """Download from a known direct TTF URL (google/fonts GitHub repo).  Returns path or None."""
    family_urls = _DIRECT_FONT_URLS.get(family)
    if not family_urls:
        return None
    # Try requested style, then fall back to Regular
    url = family_urls.get(style) or family_urls.get('Regular')
    if not url:
        return None
    logger.info(f"Downloading '{family}' from direct URL: {url}")
    try:
        font_bytes = _fetch_bytes(url)
        if font_bytes[:4] not in _VALID_FONT_MAGIC:
            logger.warning(
                f"Direct URL returned non-TTF data for '{family}' "
                f"(header: {font_bytes[:8]!r}) — skipping"
            )
            return None
        dest.write_bytes(font_bytes)
        logger.info(f"Cached '{family}' from direct URL → {dest}")
        return str(dest)
    except Exception as e:
        logger.warning(f"Direct URL download failed for '{family}': {e}")
        return None


def _download_google_font(family: str, bold: bool, italic: bool, dest: Path) -> str | None:
    """Download a Google Font and save to dest.  Returns path or None."""
    # Use v1 CSS API with IE8 UA — returns a TTF src URL directly.
    # v2 (css2) returns WOFF2 which Pillow cannot load.
    #
    # Weight variants: use numeric codes (400/700), optionally with 'italic' suffix.
    # Some fonts (e.g. Bebas Neue) only have one weight — try the requested weight
    # first, then fall back to 400 if the API returns 400 Bad Request.
    if bold and italic:
        variants_to_try = ['700italic', '400italic', '700', '400']
    elif bold:
        variants_to_try = ['700', '400']
    elif italic:
        variants_to_try = ['400italic', '700italic', '400']
    else:
        variants_to_try = ['400', '700']
    css = None
    for variant in variants_to_try:
        api_url = _GFONTS_CSS_V1.format(
            family=urllib.parse.quote(family), variant=variant
        )
        try:
            css = _fetch_text(api_url)
            break
        except urllib.error.HTTPError as e:
            if e.code == 400:
                logger.debug(f"Variant {variant} not available for '{family}' (HTTP 400), trying next weight")
                continue
            logger.warning(f"Could not fetch Google Fonts CSS for '{family}': {e}")
            return None
        except Exception as e:
            logger.warning(f"Could not fetch Google Fonts CSS for '{family}': {e}")
            return None

    if css is None:
        logger.warning(f"No valid weight variant found for '{family}'")
        return None

    # v1 API returns a TTF src URL for IE8 UA
    ttf_url = _extract_font_url(css, suffixes=('.ttf', '.TTF'))
    if not ttf_url:
        # Fallback: grab any font URL from the CSS
        for m in re.finditer(r'url\((https://[^)]+)\)', css):
            ttf_url = m.group(1)
            break
    if not ttf_url:
        logger.warning(f"No font URL found in Google Fonts CSS for '{family}' ({variant})")
        return None

    try:
        font_bytes = _fetch_bytes(ttf_url)
        # Positive validity check — only save if magic bytes confirm TTF/OTF.
        # Google Fonts now returns WOFF2 (or other formats) regardless of UA.
        if font_bytes[:4] not in _VALID_FONT_MAGIC:
            logger.warning(
                f"Google Fonts returned non-TTF data for '{family}' "
                f"(header: {font_bytes[:8]!r}) — skipping"
            )
            return None
        dest.write_bytes(font_bytes)
        logger.info(f"Cached '{family}' ({variant}) → {dest}")
        return str(dest)
    except Exception as e:
        logger.warning(f"Could not download font file for '{family}': {e}")
        return None


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode('utf-8')


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def _extract_font_url(css: str, suffixes: tuple) -> str | None:
    """Pull the first font src URL matching one of the given file suffixes."""
    for m in re.finditer(r'url\((https://[^)]+)\)', css):
        u = m.group(1)
        if any(u.lower().endswith(s) for s in suffixes):
            return u
    return None


def get_pil_font(family: str, size: int, bold: bool = False, italic: bool = False):
    """
    Return a PIL ImageFont for the given Google Font family and size.

    Falls back to bundled DejaVu / system fonts if the download fails.

    Args:
        family: Google Font family name
        size:   Point size
        bold:   Use bold weight
        italic: Use italic style

    Returns:
        PIL ImageFont (FreeType or default)
    """
    from PIL import ImageFont  # local import keeps module importable without PIL

    font_path = get_font_path(family, bold, italic)
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except Exception as e:
            logger.warning(f"PIL could not load font '{font_path}': {e}")

    # System / bundled fallbacks
    for fb in _SYSTEM_FALLBACKS:
        if os.path.exists(fb):
            try:
                return ImageFont.truetype(fb, size)
            except Exception:
                pass

    logger.warning(f"All font fallbacks failed for '{family}', using PIL default")
    return ImageFont.load_default()
