from flask import Blueprint, jsonify, request, render_template, Response, current_app
from utilities.settings import load_config, validate_url, save_config
from utilities.settings_schema import SETTINGS_SCHEMA
import logging
from queues.config_manager import add_scraper, clean_notifications, get_content_source_settings, update_content_source, get_version_settings, add_content_source, delete_content_source, save_config, get_enabled_content_sources
from routes.models import admin_required, onboarding_required
from .utils import is_user_system_enabled
import traceback
import json
import os
import platform
from datetime import datetime
from routes.notifications import (
    send_telegram_notification, 
    send_discord_notification, 
    send_ntfy_notification, 
    send_email_notification
)
import re
import time
from content_checkers.trakt import fetch_liked_trakt_lists_details
from database.database_writing import update_version_name # Ensure this is imported
import sys

settings_bp = Blueprint('settings', __name__)

# --- BEGIN Hardcoded Default Versions ---
HARDCODED_DEFAULT_VERSIONS = {
  "versions": {
    "2160p REMUX": {
      "bitrate_weight": "3",
      "enable_hdr": True,
      "filter_in": [
        "REMUX|Remux"
      ],
      "filter_out": [
        "\\b(Fre|Ger|Ita|Spa|Cze|Hun|Pol|Rus|Ukr|MULTI)\\b",
        "\\b(SDR)\\b",
        "\\b(BEN.THE.MEN)\\b",
        "\\b(ESP|Esp|LATINO)\\b",
        "\\b(RGzsRutracker)\\b",
        "\\b(3D)\\b",
        "www"
      ],
      "hdr_weight": "3",
      "max_resolution": "2160p",
      "max_size_gb": None,
      "min_size_gb": 0,
      "min_bitrate_mbps": 0.01,
      "max_bitrate_mbps": float('inf'),
      "preferred_filter_in": [
        [
          "\\b(3L|BiZKiT|BLURANiUM|BMF|CiNEPHiLES|FraMeSToR|PmP|WiLDCAT|ZQ)\\b",
          2000
        ],
        [
          "\\b(Flights|NCmt|playBD|SiCFoI|SURFINBIRD|TEPES)\\b",
          1500
        ],
        [
          "\\b(decibeL|EPSiLON|HiFi|iFT|KRaLiMaRKo|PTP|SumVision|TOA|TRiToN|NTb)\\b",
          1000
        ],
        [
          "\\b(DV)\\b",
          1000
        ],
        [
          "\\b(FGT|NOGRP)\\b",
          500
        ]
      ],
      "preferred_filter_out": [
        [
          "[^\\x00-\\x7F\u00c5\u00c4\u00d6\u00e5\u00e4\u00f6]",
          10000
        ]
      ],
      "require_physical_release": True,
      "resolution_wanted": "==",
      "resolution_weight": "3",
      "similarity_threshold": "0.8",
      "similarity_threshold_anime": "0.35",
      "similarity_weight": "3",
      "size_weight": "3",
      "wake_count": 0,
      "language_code": "en",
      "fallback_version": "None",
      "year_match_weight": 3,
      "anime_filter_mode": "None"
    },
    "1080p REMUX": {
      "bitrate_weight": "3",
      "enable_hdr": False,
      "filter_in": [
        "REMUX|Remux"
      ],
      "filter_out": [
        "\\b(HONE|MP4|SDR|Rus|Russian|BenTheMen|Ben.The.Men)\\b",
        "\\b(x265|HEVC|h265)\\b",
        "RGzsRutracker",
        "www"
      ],
      "hdr_weight": "3",
      "max_resolution": "1080p",
      "max_size_gb": None,
      "min_size_gb": 0,
      "min_bitrate_mbps": 0.01,
      "max_bitrate_mbps": float('inf'),
      "preferred_filter_in": [
        [
          "\\b(3L|BiZKiT|BLURANiUM|BMF|CiNEPHiLES|FraMeSToR|PmP|WiLDCAT|ZQ)\\b",
          2000
        ],
        [
          "\\b(Flights|NCmt|playBD|SiCFoI|SURFINBIRD|TEPES)\\b",
          1500
        ],
        [
          "\\b(decibeL|EPSiLON|HiFi|iFT|KRaLiMaRKo|PTP|SumVision|TOA|TRiToN|NTb)\\b",
          1000
        ],
        [
          "\\b(FGT|NOGRP)\\b",
          500
        ]
      ],
      "preferred_filter_out": [
        [
          "[^\\x00-\\x7F\u00c5\u00c4\u00d6\u00e5\u00e4\u00f6]",
          10000
        ]
      ],
      "require_physical_release": True,
      "resolution_wanted": "==",
      "resolution_weight": "3",
      "similarity_threshold": "0.8",
      "similarity_threshold_anime": "0.35",
      "similarity_weight": "3",
      "size_weight": "3",
      "wake_count": 0,
      "language_code": "en",
      "fallback_version": "None",
      "year_match_weight": 3,
      "anime_filter_mode": "None"
    },
    "1080p WEB": {
      "bitrate_weight": "3",
      "enable_hdr": False,
      "filter_in": [
        "WEB|Web"
      ],
      "filter_out": [
        "\\b(x265|HEVC|MP4|mp4|HDR|H265|h256|3D|10bit)\\b",
        "RGzsRutracker",
        "www.Torrenting.com"
      ],
      "hdr_weight": "3",
      "max_resolution": "1080p",
      "max_size_gb": None,
      "min_size_gb": 0,
      "min_bitrate_mbps": 0.01,
      "max_bitrate_mbps": float('inf'),
      "preferred_filter_in": [
        [
          "\\b(WEBRip)\\b",
          100
        ],
        [
          "\\b(ABBIE|AJP69|APEX|PAXA|PEXA|XEPA|BLUTONiUM|CMRG|CRFW|CRUD|FLUX|GNOME|HONE|KiNGS|Kitsune|NOSiViD|NTb|NTG|SiC|TEPES)\\b",
          500
        ],
        [
          "\\b(dB|Flights|MiU|monkee|MZABI|PHOENiX|playWEB|SbR|SMURF|TOMMY|XEBEC|4KBEC|CEBEX)\\b",
          400
        ],
        [
          "\\b(BYNDR|GNOMiSSiON|NINJACENTRAL|ROCCaT|SiGMA|SLiGNOME|SwAgLaNdEr)\\b",
          300
        ],
        [
          "\\b(WEB|WEB-DL|Web-DL)\\b",
          250
        ],
        [
          "\\b(CAKES|GGEZ|GGWP|GLHF|GOSSIP|NAISU|KOGI|PECULATE|SLOT|EDITH|ETHEL|ELEANOR|B2B|SPAMnEGGS|FTP|DiRT|SYNCOPY|BAE|SuccessfulCrab|NHTFS|SURCODE|B0MBARDIERS|DEFLATE|INFLATE)\\b",
          480
        ],
        [
          "\\b(Arg0|BTW|CasStudio|CiT|Coo7|DEEP|DRACULA|END|ETHiCS|FC|FGT|HDT|iJP|iKA|iT00NZ|JETIX|KHN|KiMCHI|LAZY|Legion|legi0n|LYS1TH3A|NPMS|NYH|OZR|orbitron|PSiG|RTFM|RTN|SCY|SDCC|SPiRiT|T4H|T6D|TVSmash|Vanilla|ViSiON|ViSUM|Vodes|WELP|ZeroBuild|NOGRP|BTN)\\b",
          100
        ]
      ],
      "preferred_filter_out": [
        [
          "\\b(MULTI)\\b",
          200
        ],
        [
          "\\b(RUS|RUSSIAN|Russian|Rus|PLSUB|Ger|Esp|Fre|Latino|LATINO|SPANISH|HINDI|KAZAKH|ITA|POR|Cze|Hun|Pol|Ukr)\\b",
          1000
        ],
        [
          "[^\\x00-\\x7F\u00c5\u00c4\u00d6\u00e5\u00e4\u00f6]",
          10000
        ],
        [
          "www",
          1000
        ]
      ],
      "require_physical_release": False,
      "resolution_wanted": "==",
      "resolution_weight": "3",
      "similarity_threshold": "0.8",
      "similarity_threshold_anime": "0.35",
      "similarity_weight": "3",
      "size_weight": "3",
      "wake_count": 0,
      "language_code": "en",
      "fallback_version": "None",
      "year_match_weight": 3,
      "anime_filter_mode": "None"
    },
    "2160p WEB": {
      "bitrate_weight": "3",
      "enable_hdr": True,
      "filter_in": [
        "WEB|Web"
      ],
      "filter_out": [
        "\\b(MP4|mp4|3D|SDR)\\b",
        "www",
        "RGzsRutracker",
        "www.Torrenting.com"
      ],
      "hdr_weight": "3",
      "max_resolution": "2160p",
      "max_size_gb": None,
      "min_size_gb": 0,
      "min_bitrate_mbps": 0.01,
      "max_bitrate_mbps": float('inf'),
      "preferred_filter_in": [
        [
          "\\b(AMZN)\\b",
          5
        ],
        [
          "DV",
          50
        ],
        [
          "DV.HDR.2160p.WEB.H265",
          25
        ],
        [
          "DV.2160p.WEB.H265",
          50
        ],
        [
          "\\b(WEBRip)\\b",
          100
        ],
        [
          "\\b(ABBIE|AJP69|APEX|PAXA|PEXA|XEPA|BLUTONiUM|CMRG|CRFW|CRUD|FLUX|GNOME|HONE|KiNGS|Kitsune|NOSiViD|NTb|NTG|SiC|TEPES)\\b",
          500
        ],
        [
          "\\b(dB|Flights|MiU|monkee|MZABI|PHOENiX|playWEB|SbR|SMURF|TOMMY|XEBEC|4KBEC|CEBEX)\\b",
          400
        ],
        [
          "\\b(BYNDR|GNOMiSSiON|NINJACENTRAL|ROCCaT|SiGMA|SLiGNOME|SwAgLaNdEr)\\b",
          300
        ],
        [
          "\\b(WEB|WEB-DL|Web-DL)\\b",
          250
        ],
        [
          "\\b(CAKES|GGEZ|GGWP|GLHF|GOSSIP|NAISU|KOGI|PECULATE|SLOT|EDITH|ETHEL|ELEANOR|B2B|SPAMnEGGS|FTP|DiRT|SYNCOPY|BAE|SuccessfulCrab|NHTFS|SURCODE|B0MBARDIERS|DEFLATE|INFLATE)\\b",
          495
        ],
        [
          "\\b(Arg0|BTW|CasStudio|CiT|Coo7|DEEP|DRACULA|END|ETHiCS|FC|FGT|HDT|iJP|iKA|iT00NZ|JETIX|KHN|KiMCHI|LAZY|Legion|legi0n|LYS1TH3A|NPMS|NYH|OZR|orbitron|PSiG|RTFM|RTN|SCY|SDCC|SPiRiT|T4H|T6D|TVSmash|Vanilla|ViSiON|ViSUM|Vodes|WELP|ZeroBuild|NOGRP|BTN)\\b",
          100
        ]
      ],
      "preferred_filter_out": [
        [
          "[^\\x00-\\x7F\u00c5\u00c4\u00d6\u00e5\u00e4\u00f6]",
          10000
        ],
        [
          "\\b(RUS|RUSSIAN|Russian|Rus|PLSUB|Ger|Esp|Fre|Latino|LATINO|SPANISH|HINDI|KAZAKH|ITA|POR|Cze|Hun|Pol|Ukr)\\b",
          1000
        ],
        [
          "\\b(MULTI)\\b",
          200
        ]
      ],
      "require_physical_release": False,
      "resolution_wanted": "==",
      "resolution_weight": "3",
      "similarity_threshold": "0.8",
      "similarity_threshold_anime": "0.35",
      "similarity_weight": "3",
      "size_weight": "3",
      "wake_count": 0,
      "language_code": "en",
      "fallback_version": "None",
      "year_match_weight": 3,
      "anime_filter_mode": "None"
    },
    "1080p ENCODE": {
      "enable_hdr": False,
      "max_resolution": "1080p",
      "resolution_wanted": "==",
      "resolution_weight": 3,
      "hdr_weight": 3,
      "similarity_weight": 3,
      "size_weight": 3,
      "bitrate_weight": 3,
      "preferred_filter_in": [],
      "preferred_filter_out": [],
      "filter_in": [
        "1080p.BluRay|1080p.bluray"
      ],
      "filter_out": [
        "x265",
        "h265",
        "hevc",
        "HEVC"
      ],
      "min_size_gb": 0,
      "max_size_gb": None,
      "min_bitrate_mbps": 0.01,
      "max_bitrate_mbps": float('inf'),
      "similarity_threshold_anime": 0.35,
      "similarity_threshold": 0.8,
      "wake_count": 0,
      "require_physical_release": True,
      "language_code": "en",
      "fallback_version": "None",
      "year_match_weight": 3,
      "anime_filter_mode": "None"
    },
    "2160p BEST": {
      "bitrate_weight": "3",
      "enable_hdr": True,
      "filter_in": [],
      "filter_out": [
        "\\b(MP4|mp4|3D|SDR)\\b",
        "www",
        "RGzsRutracker",
        "www.Torrenting.com"
      ],
      "hdr_weight": "3",
      "max_resolution": "2160p",
      "max_size_gb": None,
      "min_size_gb": 0,
      "min_bitrate_mbps": 0.01,
      "max_bitrate_mbps": float('inf'),
      "preferred_filter_in": [
        [
          "\\b(WEBRip)\\b",
          100
        ],
        [
          "DV",
          50
        ],
        [
          "REPACK",
          50
        ],
        [
          "DV.HDR.2160p.WEB.H265",
          25
        ],
        [
          "HYBRID",
          25
        ],
        [
          "DV.2160p.WEB.H265",
          50
        ],
        [
          "\\b(3L|BiZKiT|BLURANiUM|BMF|CiNEPHiLES|FraMeSToR|PmP|WiLDCAT|ZQ)\\b",
          2000
        ],
        [
          "\\b(Flights|NCmt|playBD|SiCFoI|SURFINBIRD|TEPES)\\b",
          1800
        ],
        [
          "\\b(decibeL|EPSiLON|HiFi|iFT|KRaLiMaRKo|PTP|SumVision|TOA|TRiToN)\\b",
          1600
        ],
        [
          "REMUX|Remux",
          1500
        ],
        [
          "\\b(BBQ|BMF|c0kE|Chotab|CRiSC|CtrlHD|D-Z0N3|Dariush|decibeL|DON|EbP|EDPH|Geek|LolHD|NCmt|PTer|TayTO|TDD|TnP|VietHD|ZQ)\\b",
          1000
        ],
        [
          "\\b(EA|HiDt|HiSD|iFT|QOQ|SA89|sbR)\\b",
          900
        ],
        [
          "\\b(BHDStudio|hallowed|HONE|LoRD|playHD|SPHD|W4NK3R)\\b",
          800
        ],
        [
          "\\b(Bluray|BLURAY)\\b",
          500
        ],
        [
          "\\b(ABBIE|AJP69|APEX|PAXA|PEXA|XEPA|BLUTONiUM|CMRG|CRFW|CRUD|FLUX|GNOME|HONE|KiNGS|Kitsune|NOSiViD|NTb|NTG|SiC|TEPES)\\b",
          500
        ],
        [
          "\\b(dB|Flights|MiU|monkee|MZABI|PHOENiX|playWEB|SbR|SMURF|TOMMY|XEBEC|4KBEC|CEBEX)\\b",
          400
        ],
        [
          "\\b(BYNDR|GNOMiSSiON|NINJACENTRAL|ROCCaT|SiGMA|SLiGNOME|SwAgLaNdEr)\\b",
          300
        ],
        [
          "\\b(WEB|WEB-DL|Web-DL)\\b",
          250
        ],
        [
          "\\b(CAKES|GGEZ|GGWP|GLHF|GOSSIP|NAISU|KOGI|PECULATE|SLOT|EDITH|ETHEL|ELEANOR|B2B|SPAMnEGGS|FTP|DiRT|SYNCOPY|BAE|SuccessfulCrab|NHTFS|SURCODE|B0MBARDIERS|DEFLATE|INFLATE)\\b",
          495
        ],
        [
          "SCENE",
          1
        ],
        [
          "\\b(Arg0|BTW|CasStudio|CiT|Coo7|DEEP|DRACULA|END|ETHiCS|FC|FGT|HDT|iJP|iKA|iT00NZ|JETIX|KHN|KiMCHI|LAZY|Legion|legi0n|LYS1TH3A|NPMS|NYH|OZR|orbitron|PSiG|RTFM|RTN|SCY|SDCC|SPiRiT|T4H|T6D|TVSmash|Vanilla|ViSiON|ViSUM|Vodes|WELP|ZeroBuild|NOGRP|BTN)\\b",
          100
        ]
      ],
      "preferred_filter_out": [
        [
          "\\b(HDR10Plus)\\b",
          25
        ],
        [
          "\\b(DVP7|P7|P7)\\b",
          100
        ],
        [
          "\\b(DVP8|P8|P8)\\b",
          100
        ],
        [
          "\\b(HDR10+)\\b",
          100
        ],
        [
          "[^\\x00-\\x7F\u00c5\u00c4\u00d6\u00e5\u00e4\u00f6]",
          10000
        ],
        [
          "\\b(RUS|RUSSIAN|Russian|Rus|PLSUB|Ger|Esp|Fre|Latino|LATINO|SPANISH|HINDI|KAZAKH|ITA|POR|Cze|Hun|Pol|Ukr)\\b",
          1000
        ],
        [
          "\\b(MULTI)\\b",
          200
        ]
      ],
      "require_physical_release": False,
      "resolution_wanted": "==",
      "resolution_weight": "3",
      "similarity_threshold": "0.8",
      "similarity_threshold_anime": "0.35",
      "similarity_weight": "3",
      "size_weight": "3",
      "wake_count": 0,
      "language_code": "en",
      "fallback_version": "None",
      "year_match_weight": 3,
      "anime_filter_mode": "None"
    },
    "1080p BEST": {
      "bitrate_weight": "3",
      "enable_hdr": False,
      "filter_in": [],
      "filter_out": [
        "\\b(x265|HEVC|MP4|mp4|HDR|H265|h256|3D|10bit)\\b",
        "RGzsRutracker"
      ],
      "hdr_weight": "3",
      "max_resolution": "1080p",
      "max_size_gb": None,
      "min_size_gb": 0,
      "min_bitrate_mbps": 0.01,
      "max_bitrate_mbps": float('inf'),
      "preferred_filter_in": [
        [
          "\\b(WEBRip)\\b",
          100
        ],
        [
          "REMUX|Remux",
          100
        ],
        [
          "NORDIC",
          25
        ],
        [
          "REPACK",
          50
        ],
        [
          "\\b(3L|BiZKiT|BLURANiUM|BMF|CiNEPHiLES|FraMeSToR|PmP|WiLDCAT|ZQ)\\b",
          2000
        ],
        [
          "\\b(Flights|NCmt|playBD|SiCFoI|SURFINBIRD|TEPES)\\b",
          1800
        ],
        [
          "\\b(decibeL|EPSiLON|HiFi|iFT|KRaLiMaRKo|PTP|SumVision|TOA|TRiToN)\\b",
          1600
        ],
        [
          "REMUX|Remux",
          1500
        ],
        [
          "\\b(BBQ|BMF|c0kE|Chotab|CRiSC|CtrlHD|D-Z0N3|Dariush|decibeL|DON|EbP|EDPH|Geek|LolHD|NCmt|PTer|TayTO|TDD|TnP|VietHD|ZQ)\\b",
          1000
        ],
        [
          "\\b(EA|HiDt|HiSD|iFT|QOQ|SA89|sbR)\\b",
          900
        ],
        [
          "\\b(BHDStudio|hallowed|HONE|LoRD|playHD|SPHD|W4NK3R)\\b",
          800
        ],
        [
          "\\b(Bluray|BLURAY)\\b",
          500
        ],
        [
          "\\b(ABBIE|AJP69|APEX|PAXA|PEXA|XEPA|BLUTONiUM|CMRG|CRFW|CRUD|FLUX|GNOME|HONE|KiNGS|Kitsune|NOSiViD|NTb|NTG|SiC|TEPES)\\b",
          500
        ],
        [
          "\\b(dB|Flights|MiU|monkee|MZABI|PHOENiX|playWEB|SbR|SMURF|TOMMY|XEBEC|4KBEC|CEBEX)\\b",
          400
        ],
        [
          "\\b(BYNDR|GNOMiSSiON|NINJACENTRAL|ROCCaT|SiGMA|SLiGNOME|SwAgLaNdEr)\\b",
          300
        ],
        [
          "\\b(WEB|WEB-DL|Web-DL)\\b",
          250
        ],
        [
          "\\b(CAKES|GGEZ|GGWP|GLHF|GOSSIP|NAISU|KOGI|PECULATE|SLOT|EDITH|ETHEL|ELEANOR|B2B|SPAMnEGGS|FTP|DiRT|SYNCOPY|BAE|SuccessfulCrab|NHTFS|SURCODE|B0MBARDIERS|DEFLATE|INFLATE)\\b",
          480
        ],
        [
          "SCENE",
          1
        ],
        [
          "\\b(Arg0|BTW|CasStudio|CiT|Coo7|DEEP|DRACULA|END|ETHiCS|FC|FGT|HDT|iJP|iKA|iT00NZ|JETIX|KHN|KiMCHI|LAZY|Legion|legi0n|LYS1TH3A|NPMS|NYH|OZR|orbitron|PSiG|RTFM|RTN|SCY|SDCC|SPiRiT|T4H|T6D|TVSmash|Vanilla|ViSiON|ViSUM|Vodes|WELP|ZeroBuild|NOGRP|BTN)\\b",
          100
        ]
      ],
      "preferred_filter_out": [
        [
          "\\b(MULTI)\\b",
          200
        ],
        [
          "\\b(RUS|RUSSIAN|Russian|Rus|PLSUB|Ger|Esp|Fre|Latino|LATINO|SPANISH|HINDI|KAZAKH|ITA|POR|Cze|Hun|Pol|Ukr)\\b",
          1000
        ],
        [
          "BDRip",
          1000
        ],
        [
          "EN-TR",
          1000
        ],
        [
          "www",
          1000
        ],
        [
          "[^\\x00-\\x7F\u00c5\u00c4\u00d6\u00e5\u00e4\u00f6]",
          10000
        ]
      ],
      "require_physical_release": False,
      "resolution_wanted": "==",
      "resolution_weight": "3",
      "similarity_threshold": "0.8",
      "similarity_threshold_anime": "0.35",
      "similarity_weight": "3",
      "size_weight": "3",
      "wake_count": 0,
      "language_code": "en",
      "fallback_version": "None",
      "year_match_weight": 3,
      "anime_filter_mode": "None"
    }
  }
}
# --- END Hardcoded Default Versions ---

@settings_bp.route('/content-sources/content')
def content_sources_content():
    config = load_config()
    source_types = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())
    return render_template('settings_tabs/content_sources.html', 
                           settings=config, 
                           source_types=source_types, 
                           settings_schema=SETTINGS_SCHEMA)

@settings_bp.route('/content-sources/types')
def content_sources_types():
    config = load_config()
    source_types_from_schema = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())
         
    return jsonify({
        'source_types': source_types_from_schema, 
        'settings': SETTINGS_SCHEMA['Content Sources']['schema']
    })

@settings_bp.route('/content-sources/trakt-friends')
def get_trakt_friends():
    """Get a list of authorized Trakt friends for the dropdown"""
    try:
        friends = []
        trakt_friends_dir = os.environ.get('USER_CONFIG', '/user/config')
        trakt_friends_dir = os.path.join(trakt_friends_dir, 'trakt_friends')
        
        # List all files in the trakt_friends_dir
        if os.path.exists(trakt_friends_dir):
            for filename in os.listdir(trakt_friends_dir):
                if filename.endswith('.json'):
                    try:
                        # Extract auth_id from filename
                        auth_id = filename.replace('.json', '')
                        
                        with open(os.path.join(trakt_friends_dir, filename), 'r') as f:
                            state = json.load(f)
                        
                        # Only include authorized accounts
                        if state.get('status') == 'authorized':
                            friends.append({
                                'auth_id': auth_id,
                                'friend_name': state.get('friend_name', 'Unknown Friend'),
                                'username': state.get('username', ''),
                                'display_name': f"{state.get('friend_name', 'Unknown Friend')}'s Watchlist"
                            })
                    except Exception as e:
                        logging.error(f"Error reading friend state file {filename}: {str(e)}")
        
        return jsonify({
            'success': True,
            'friends': friends
        })
    
    except Exception as e:
        logging.error(f"Error listing Trakt friends: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/content_sources/add', methods=['POST'])
@admin_required
def add_content_source_route():
    try:
        if request.is_json:
            source_config = request.json
        else:
            return jsonify({'success': False, 'error': f'Unsupported Content-Type: {request.content_type}'}), 415
        
        source_type = source_config.pop('type', None)
        if not source_type:
            return jsonify({'success': False, 'error': 'No source type provided'}), 400

        # Ensure versions is a dictionary (new standard)
        if 'versions' in source_config:
            # Convert list to dict if necessary (handle old format)
            if isinstance(source_config['versions'], list):
                 source_config['versions'] = {v: True for v in source_config['versions']}
            # Ensure it's a dict, otherwise default to empty dict
            elif not isinstance(source_config['versions'], dict):
                 logging.warning(f"Invalid 'versions' format for {source_type}: {source_config['versions']}. Resetting to empty dict.")
                 source_config['versions'] = {}
        
        new_source_id = add_content_source(source_type, source_config)
        
        return jsonify({'success': True, 'source_id': new_source_id})
    except Exception as e:
        logging.error(f"Error adding content source: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    
@settings_bp.route('/content_sources/delete', methods=['POST'])
@admin_required
def delete_content_source_route():
    source_id = request.json.get('source_id')
    if not source_id:
        return jsonify({'success': False, 'error': 'No source ID provided'}), 400

    logging.info(f"Attempting to delete content source: {source_id}")
    
    success = delete_content_source(source_id)
    
    if success:
        # Update the config in web_server.py
        config = load_config()
        if 'Content Sources' in config and source_id in config['Content Sources']:
            del config['Content Sources'][source_id]
            save_config(config)
        
        logging.info(f"Content source {source_id} deleted successfully")
        return jsonify({'success': True})
    else:
        logging.warning(f"Failed to delete content source: {source_id}")
        return jsonify({'success': False, 'error': 'Source not found or already deleted'}), 404

@settings_bp.route('/scrapers/add', methods=['POST'])
@admin_required
def add_scraper_route():
    logging.info(f"Received request to add scraper. Content-Type: {request.content_type}")
    logging.info(f"Request data: {request.data}")
    try:
        if request.is_json:
            scraper_config = request.json
        else:
            return jsonify({'success': False, 'error': f'Unsupported Content-Type: {request.content_type}'}), 415
        
        logging.info(f"Parsed data: {scraper_config}")
        
        if not scraper_config:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        scraper_type = scraper_config.pop('type', None)
        if not scraper_type:
            return jsonify({'success': False, 'error': 'No scraper type provided'}), 400
        
        new_scraper_id = add_scraper(scraper_type, scraper_config)
        
        # Log the updated config after adding the scraper
        updated_config = load_config()
        logging.info(f"Updated config after adding scraper: {updated_config}")
        
        return jsonify({'success': True, 'scraper_id': new_scraper_id})
    except Exception as e:
        logging.error(f"Error adding scraper: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    
@settings_bp.route('/scrapers/content')
def scrapers_content():
    try:
        settings = load_config()
        scraper_types = list(SETTINGS_SCHEMA["Scrapers"]["schema"].keys())        
        scraper_settings = {scraper: list(SETTINGS_SCHEMA["Scrapers"]["schema"][scraper].keys()) for scraper in SETTINGS_SCHEMA["Scrapers"]["schema"]}
        return render_template('settings_tabs/scrapers.html', settings=settings, scraper_types=scraper_types, scraper_settings=scraper_settings)
    except Exception as e:
        return jsonify({'error': 'An error occurred while loading scraper settings'}), 500

@settings_bp.route('/scrapers/get', methods=['GET'])
def get_scrapers():
    config = load_config()
    scraper_types = list(SETTINGS_SCHEMA["Scrapers"]["schema"].keys())        
    return render_template('settings_tabs/scrapers.html', settings=config, scraper_types=scraper_types)

@settings_bp.route('/get_content_source_types', methods=['GET'])
def get_content_source_types():
    content_sources = SETTINGS_SCHEMA['Content Sources']['schema']
    return jsonify({
        'source_types': list(content_sources.keys()),
        'settings': content_sources
    })

@settings_bp.route('/scrapers/delete', methods=['POST'])
@admin_required
def delete_scraper():
    data = request.json
    scraper_id = data.get('scraper_id')
    
    if not scraper_id:
        return jsonify({'success': False, 'error': 'No scraper ID provided'}), 400

    config = load_config()
    scrapers = config.get('Scrapers', {})
    
    if scraper_id in scrapers:
        del scrapers[scraper_id]
        config['Scrapers'] = scrapers
        save_config(config)
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Scraper not found'}), 404
    
@settings_bp.route('/notifications/delete', methods=['POST'])
@admin_required
def delete_notification():
    try:
        notification_id = request.json.get('notification_id')
        if not notification_id:
            return jsonify({'success': False, 'error': 'No notification ID provided'}), 400

        config = load_config()
        if 'Notifications' in config and notification_id in config['Notifications']:
            del config['Notifications'][notification_id]
            save_config(config)
            logging.info(f"Notification {notification_id} deleted successfully")
            return jsonify({'success': True})
        else:
            logging.warning(f"Failed to delete notification: {notification_id}")
            return jsonify({'success': False, 'error': 'Notification not found'}), 404
    except Exception as e:
        logging.error(f"Error deleting notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/notifications/add', methods=['POST'])
@admin_required
def add_notification():
    try:
        notification_data = request.json
        if not notification_data or 'type' not in notification_data:
            return jsonify({'success': False, 'error': 'Invalid notification data'}), 400

        config = load_config()
        if 'Notifications' not in config:
            config['Notifications'] = {}

        notification_type = notification_data['type']
        existing_count = sum(1 for key in config['Notifications'] if key.startswith(f"{notification_type}_"))
        notification_id = f"{notification_type}_{existing_count + 1}"

        notification_title = notification_type.replace('_', ' ').title()

        config['Notifications'][notification_id] = {
            'type': notification_type,
            'enabled': True,
            'title': notification_title,
            'notify_on': {
                'collected': True,
                'wanted': False,
                'scraping': False,
                'adding': False,
                'checking': False,
                'sleeping': False,
                'unreleased': False,
                'blacklisted': False,
                'pending_uncached': False,
                'upgrading': False,
                'program_stop': True,
                'program_crash': True,
                'program_start': True,
                'program_pause': True,
                'program_resume': True,
                'queue_pause': True,
                'queue_resume': True,
                'queue_start': True,
                'queue_stop': True
            }
        }

        # Add default values based on the notification type
        if notification_type == 'Telegram':
            config['Notifications'][notification_id].update({
                'bot_token': '',
                'chat_id': ''
            })
        elif notification_type == 'Discord':
            config['Notifications'][notification_id].update({
                'webhook_url': ''
            })
        elif notification_type == 'NTFY':
            config['Notifications'][notification_id].update({
                'host': '',
                'topic': '',
                'api_key': '',
                'priority': ''
            })
        elif notification_type == 'Email':
            config['Notifications'][notification_id].update({
                'smtp_server': '',
                'smtp_port': 587,
                'smtp_username': '',
                'smtp_password': '',
                'from_address': '',
                'to_address': ''
            })

        save_config(config)

        logging.info(f"Notification {notification_id} added successfully")
        return jsonify({'success': True, 'notification_id': notification_id})
    except Exception as e:
        logging.error(f"Error adding notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

def ensure_notification_defaults(notification_config):
    """Ensure notification config has all required default fields."""
    default_categories = {
        'collected': True,
        'wanted': False,
        'scraping': False,
        'adding': False,
        'checking': False,
        'sleeping': False,
        'unreleased': False,
        'blacklisted': False,
        'pending_uncached': False,
        'upgrading': False,
        'program_stop': True,
        'program_crash': True,
        'program_start': True,
        'program_pause': True,
        'program_resume': True,
        'queue_pause': True,
        'queue_resume': True,
        'queue_start': True,
        'queue_stop': True
    }

    # If notify_on is missing or empty, set it to the default values
    if 'notify_on' not in notification_config or not notification_config['notify_on']:
        notification_config['notify_on'] = default_categories.copy()
    else:
        # Ensure all categories exist in notify_on
        for category, default_value in default_categories.items():
            if category not in notification_config['notify_on']:
                notification_config['notify_on'][category] = default_value

    return notification_config

@settings_bp.route('/notifications/content', methods=['GET'])
def notifications_content():
    try:
        config = load_config()
        notification_settings = config.get('Notifications', {})
        
        # Ensure all notifications have the required defaults
        for notification_id, notification_config in notification_settings.items():
            if notification_config is not None:
                notification_settings[notification_id] = ensure_notification_defaults(notification_config)
        
        # Always save the config to ensure defaults are persisted
        config['Notifications'] = notification_settings
        save_config(config)
        
        # Sort notifications by type and then by number
        sorted_notifications = sorted(
            notification_settings.items(),
            key=lambda x: (x[1]['type'], int(x[0].split('_')[-1]))
        )
        
        html_content = render_template(
            'settings_tabs/notifications.html',
            notification_settings=dict(sorted_notifications),
            settings_schema=SETTINGS_SCHEMA
        )
        
        return jsonify({
            'status': 'success',
            'html': html_content
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'An error occurred while generating notifications content: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500

@settings_bp.route('/', methods=['GET'])
@admin_required
@onboarding_required
def index():
    try:
        config = load_config()
        config = clean_notifications(config)
        
        # --- BEGIN Auto-fix for missing Content Source type ---
        if 'Content Sources' in config and isinstance(config['Content Sources'], dict):
            content_sources = config['Content Sources']
            valid_source_types = SETTINGS_SCHEMA.get('Content Sources', {}).get('schema', {}).keys()
            fixed_count = 0
            
            for source_id, source_config in list(content_sources.items()): # Use list() for safe iteration if modifying
                if isinstance(source_config, dict):
                    # Check if type is missing or empty
                    if not source_config.get('type'): 
                        # Try to infer type from source_id (e.g., "Overseerr_1")
                        parts = source_id.split('_')
                        if parts:
                            potential_type = parts[0]
                            # Check if the inferred type is valid according to the schema
                            if potential_type in valid_source_types:
                                source_config['type'] = potential_type
                                fixed_count += 1
                                logging.warning(f"Auto-fixed missing 'type' for Content Source '{source_id}'. Set type to '{potential_type}'.")
                            else:
                                logging.error(f"Could not auto-fix Content Source '{source_id}': Inferred type '{potential_type}' is not valid.")
                        else:
                            logging.error(f"Could not auto-fix Content Source '{source_id}': Cannot infer type from ID format.")
                else:
                     # Handle cases where the source config itself isn't a dictionary (less common)
                     logging.error(f"Invalid configuration for Content Source '{source_id}': Expected a dictionary, found {type(source_config)}. Please check settings file.")
                     # Optionally remove the invalid entry: del content_sources[source_id]
            
            if fixed_count > 0:
                 logging.info(f"Automatically corrected the 'type' field for {fixed_count} Content Source(s) for rendering.")
                 # Decide whether to save the fixes automatically:
                 # save_config(config) # Uncomment this line to persist the fixes immediately
                 # Keeping it commented means fixes are only for this page load, 
                 # user needs to save settings via UI to persist.
                 
        # --- END Auto-fix ---

        scraper_types = list(SETTINGS_SCHEMA["Scrapers"]["schema"].keys())        
        source_types_from_schema = list(SETTINGS_SCHEMA["Content Sources"]["schema"].keys())
        scraper_settings = {scraper: list(SETTINGS_SCHEMA["Scrapers"]["schema"][scraper].keys()) for scraper in SETTINGS_SCHEMA["Scrapers"]["schema"]}
        is_windows = platform.system() == 'Windows'
        content_source_settings_response = get_content_source_settings_route()
        scraping_versions = list(config.get('Scraping', {}).get('versions', {}).keys())
        logging.debug(f"[index route] Extracted scraping versions: {scraping_versions}")

        # Ensure 'Scrapers' exists in the config
        if 'Scrapers' not in config:
            config['Scrapers'] = {}
        
        # Only keep the scrapers that are actually configured
        configured_scrapers = {}
        for scraper, scraper_config in config['Scrapers'].items():
            scraper_type = scraper.split('_')[0]  # Assuming format like 'Zilean_1'
            if scraper_type in scraper_settings:
                configured_scrapers[scraper] = scraper_config
        
        config['Scrapers'] = configured_scrapers

        # Ensure 'UI Settings' exists in the config
        if 'UI Settings' not in config:
            config['UI Settings'] = {}

        if 'Sync Deletions' not in config:
            config['Sync Deletions'] = {}
        
        # Ensure 'enable_user_system' exists in 'UI Settings'
        if 'enable_user_system' not in config['UI Settings']:
            config['UI Settings']['enable_user_system'] = True  # Default to True
        
        
        # Ensure 'Content Sources' exists in the config
        if 'Content Sources' not in config:
            config['Content Sources'] = {}
        
        # Ensure each content source is a dictionary
        for source, source_config in config['Content Sources'].items():
            if not isinstance(source_config, dict):
                # Log again or handle as needed if auto-fix didn't remove it
                logging.warning(f"Content source '{source}' is not a dictionary after potential auto-fix attempt.")
                config['Content Sources'][source] = {} # Replace with empty dict to prevent template errors

        # Initialize notification_settings
        if 'Notifications' not in config:
            config['Notifications'] = {
                'Telegram': {'enabled': False, 'bot_token': '', 'chat_id': ''},
                'Discord': {'enabled': False, 'webhook_url': ''},
                'NTFY': {'enabled': False, 'host': '', 'topic': '', 'api_key': '', 'priority': ''},
                'Email': {
                    'enabled': False,
                    'smtp_server': '',
                    'smtp_port': 587,
                    'smtp_username': '',
                    'smtp_password': '',
                    'from_address': '',
                    'to_address': ''
                }
            }

        # Determine if Windows symlinks are allowed
        allow_windows_symlinks_value = config.get('Debug', {}).get('use_symlinks_on_windows', False)

        # --- Add logging before rendering ---
        overseer_source_check_route = config.get('Content Sources', {}).get('Overseerr_1', 'Overseerr_1 not found')
        logging.debug(f"[Route /] State of 'Overseerr_1' before passing to template: {json.dumps(overseer_source_check_route, indent=2)}")
        # ------------------------------------

        content_source_settings = content_source_settings_response.get_json() if isinstance(content_source_settings_response, Response) else content_source_settings_response

        # Get environment mode from environment variable
        environment_mode = os.environ.get('CLI_DEBRID_ENVIRONMENT_MODE', 'full')
        
        return render_template('settings_base.html', 
                               settings=config, 
                               notification_settings=config['Notifications'],
                               scraper_types=scraper_types, 
                               scraper_settings=scraper_settings,
                               source_types=source_types_from_schema,
                               content_source_settings=content_source_settings,
                               version_names=scraping_versions,
                               settings_schema=SETTINGS_SCHEMA,
                               is_windows=is_windows,
                               allow_windows_symlinks=allow_windows_symlinks_value,
                               environment_mode=environment_mode)
    except Exception as e:
        current_app.logger.error(f"Error in settings route: {str(e)}")
        current_app.logger.error(traceback.format_exc())
        return render_template('error.html', error_message="An error occurred while loading settings."), 500
    
@settings_bp.route('/api/program_settings', methods=['GET'])
@admin_required
def api_program_settings():
    try:
        config = load_config()
        
        def merge_defaults(config_section, schema_section):
            if not isinstance(schema_section, dict):
                return config_section
                
            result = config_section.copy() if config_section else {}
            
            # Handle schema sections with explicit type and default
            if 'type' in schema_section and 'default' in schema_section:
                if not config_section:  # If no user value, use default
                    return schema_section['default']
            
            # Handle nested schema sections
            for key, schema_value in schema_section.items():
                if isinstance(schema_value, dict):
                    if 'default' in schema_value and key not in result:
                        result[key] = schema_value['default']
                    elif 'schema' in schema_value:
                        if key not in result:
                            result[key] = {}
                        result[key] = merge_defaults(result.get(key, {}), schema_value['schema'])
            
            return result

        # Merge defaults for each section
        program_settings = {}
        sections_to_include = ['Scrapers', 'Content Sources', 'Debug', 'Plex', 'Metadata Battery', 'Debrid Provider']
        
        for section in sections_to_include:
            if section in SETTINGS_SCHEMA:
                program_settings[section] = merge_defaults(
                    config.get(section, {}),
                    SETTINGS_SCHEMA[section]
                )
            else:
                program_settings[section] = config.get(section, {})

        return jsonify(program_settings)
    except Exception as e:
        logging.error(f"Error in api_program_settings: {str(e)}", exc_info=True)
        return jsonify({"error": "An error occurred while loading program settings."}), 500
    
@settings_bp.route('/scraping/get')
def get_scraping_settings():
    config = load_config()
    scraping_settings = config.get('Scraping', {})
    return jsonify(scraping_settings)

@settings_bp.route('/api/settings', methods=['POST'])
@admin_required
def update_settings():
    try:
        new_settings = request.json
        config = load_config()
        
        logging.info("Received settings update request.")
        # Optional: Log specific sections if needed for debugging
        # logging.info(f"File Management: {json.dumps(new_settings.get('File Management', {}), indent=2)}")
        # logging.info(f"Plex: {json.dumps(new_settings.get('Plex', {}), indent=2)}")
        # logging.info(f"Staleness Threshold section: {json.dumps(new_settings.get('Staleness Threshold', {}), indent=2)}")

        # --- REMOVED BLOCK ---
        # The block that directly modified settings.json for staleness_threshold
        # has been removed. The value should be handled by the main config update below.
        # --- END REMOVED BLOCK ---

        # Process Plex library strings if they are being updated
        if 'Plex' in new_settings:
            plex_updates = new_settings['Plex']
            if 'movie_libraries' in plex_updates and isinstance(plex_updates['movie_libraries'], str):
                raw_movie_libraries = plex_updates['movie_libraries']
                plex_updates['movie_libraries'] = ','.join([lib.strip() for lib in raw_movie_libraries.split(',') if lib.strip()]) if raw_movie_libraries else ''
            if 'shows_libraries' in plex_updates and isinstance(plex_updates['shows_libraries'], str):
                raw_shows_libraries = plex_updates['shows_libraries']
                plex_updates['shows_libraries'] = ','.join([lib.strip() for lib in raw_shows_libraries.split(',') if lib.strip()]) if raw_shows_libraries else ''


        # Validate Plex libraries if Plex is selected
        file_management = new_settings.get('File Management', config.get('File Management', {})) # Use existing config as fallback
        if file_management.get('file_collection_management') == 'Plex':
            # Use potentially updated plex_settings from new_settings, or fallback to existing config
            plex_settings_for_validation = new_settings.get('Plex', config.get('Plex', {}))
            
            movie_libraries_for_validation = plex_settings_for_validation.get('movie_libraries', '').strip()
            show_libraries_for_validation = plex_settings_for_validation.get('shows_libraries', '').strip()
            
            logging.info(f"Validating Plex libraries - Movie: '{movie_libraries_for_validation}', Shows: '{show_libraries_for_validation}'")
            
            if not movie_libraries_for_validation or not show_libraries_for_validation:
                error_msg = "When using Plex as your library management system, you must specify both a movie library and a TV show library."
                logging.error(f"Settings validation failed: {error_msg}")
                return jsonify({
                    "status": "error",
                    "message": error_msg
                }), 400

        # Handle mutual exclusivity between Plex and Jellyfin/Emby settings
        # Check the Media Server Type to determine which settings to keep
        file_management_settings = new_settings.get('File Management', {})
        media_server_type = file_management_settings.get('media_server_type', '')
        
        # Check if Jellyfin/Emby settings are being updated
        debug_settings = new_settings.get('Debug', {})
        if 'emby_jellyfin_url' in debug_settings and debug_settings['emby_jellyfin_url'].strip():
            # Jellyfin/Emby URL is being set
            if media_server_type == 'jellyfin':
                # User has selected Jellyfin/Emby, clear Plex settings
                logging.info("Jellyfin/Emby URL detected and Media Server Type is Jellyfin, clearing Plex settings")
                
                # Clear Plex settings in the new_settings to prevent them from being saved
                if 'Plex' in new_settings:
                    new_settings['Plex']['url'] = ''
                    new_settings['Plex']['token'] = ''
                
                # Clear File Management Plex settings
                if 'File Management' in new_settings:
                    new_settings['File Management']['plex_url_for_symlink'] = ''
                    new_settings['File Management']['plex_token_for_symlink'] = ''
                
                # Also clear these settings in the current config to ensure they're cleared
                if 'Plex' not in config:
                    config['Plex'] = {}
                config['Plex']['url'] = ''
                config['Plex']['token'] = ''
                
                if 'File Management' not in config:
                    config['File Management'] = {}
                config['File Management']['plex_url_for_symlink'] = ''
                config['File Management']['plex_token_for_symlink'] = ''
            else:
                logging.info("Jellyfin/Emby URL detected but Media Server Type is not Jellyfin, keeping Plex settings")
            
        # Check if Plex settings are being updated
        plex_settings = new_settings.get('Plex', {})
        
        plex_url_being_set = (plex_settings.get('url', '').strip() or 
                             file_management_settings.get('plex_url_for_symlink', '').strip())
        
        if plex_url_being_set:
            if media_server_type == 'plex':
                # User has selected Plex, clear Jellyfin/Emby settings
                logging.info("Plex URL detected and Media Server Type is Plex, clearing Jellyfin/Emby settings")
                
                # Clear Jellyfin/Emby settings in the new_settings
                if 'Debug' in new_settings:
                    new_settings['Debug']['emby_jellyfin_url'] = ''
                    new_settings['Debug']['emby_jellyfin_token'] = ''
                
                # Also clear these settings in the current config
                if 'Debug' not in config:
                    config['Debug'] = {}
                config['Debug']['emby_jellyfin_url'] = ''
                config['Debug']['emby_jellyfin_token'] = ''
            else:
                logging.info("Plex URL detected but Media Server Type is not Plex, keeping Jellyfin/Emby settings")

        # Function to recursively update the main config dictionary
        def update_nested_dict(current, new):
            for key, value in new.items():
                if isinstance(value, dict) and key in current and isinstance(current[key], dict):
                     # Handle specific nested structures like 'Content Sources' or 'versions' if needed,
                     # otherwise, just recurse.
                     if key == 'Content Sources':
                         # Special handling for content sources if needed (e.g., merging vs replacing)
                         # Current behavior seems to merge/update existing and add new
                         for source_id, source_config in value.items():
                             if source_id in current[key]:
                                 # Recursively update existing source config
                                 update_nested_dict(current[key][source_id], source_config)
                             else:
                                 # Add new source config
                                 current[key][source_id] = source_config
                     elif key == 'versions' and 'Scraping' in current: # Check parent key for context
                         # Handle Scraping versions similarly
                         for version_id, version_config in value.items():
                             if version_id in current[key]:
                                 update_nested_dict(current[key][version_id], version_config)
                             else:
                                 current[key][version_id] = version_config
                     else:
                        update_nested_dict(current[key], value)
                else:
                    # Update or add the value
                    current[key] = value

        # Update the main config object with the new settings
        update_nested_dict(config, new_settings)
        
        # --- Simplified Staleness Update (if needed, relies on update_nested_dict) ---
        # If 'Staleness Threshold' exists in new_settings, update_nested_dict should handle it.
        # We might want explicit type conversion/validation *before* update_nested_dict though.
        if 'Staleness Threshold' in new_settings and 'staleness_threshold' in new_settings['Staleness Threshold']:
             try:
                 # Ensure the value in the main config dict is an int
                 config['Staleness Threshold']['staleness_threshold'] = int(new_settings['Staleness Threshold']['staleness_threshold'])
                 logging.info(f"Staleness threshold updated in main config object to: {config['Staleness Threshold']['staleness_threshold']}")
             except (ValueError, TypeError) as e:
                 logging.warning(f"Invalid value provided for staleness_threshold, cannot convert to int: {new_settings['Staleness Threshold']['staleness_threshold']}. Skipping update for this key. Error: {e}")
                 # Optionally remove the invalid key from the update or handle differently
                 # For now, update_nested_dict might have already placed the invalid value; save_config will likely fail if schema expects int.
                 # Or, revert the change in the config object if conversion fails:
                 # config['Staleness Threshold']['staleness_threshold'] = load_config().get('Staleness Threshold', {}).get('staleness_threshold', 7) # Revert to loaded or default


        # Update content source check periods (Seems correctly handled by update_nested_dict if types match)
        # If type conversion is needed (e.g., string to float):
        if 'Debug' in new_settings and 'content_source_check_period' in new_settings['Debug']:
            if 'Debug' not in config: config['Debug'] = {} # Ensure Debug section exists
            if 'content_source_check_period' not in config['Debug']: config['Debug']['content_source_check_period'] = {} # Ensure sub-section exists
            
            for source, period_str in new_settings['Debug']['content_source_check_period'].items():
                 try:
                     config['Debug']['content_source_check_period'][source] = float(period_str)
                 except (ValueError, TypeError):
                      logging.warning(f"Invalid period value '{period_str}' for source '{source}'. Skipping update.")
                      # Keep existing value or set to a default if needed

        # Handle Reverse Parser settings (Seems correctly handled by update_nested_dict)
        # Add validation if necessary

        # Save the updated main config object atomically (assuming save_config does this)
        save_config(config)
        logging.info("Main configuration saved successfully.")
        
        # Clear content source cache files
        try:
            from routes.debug_routes import get_cache_files
            cache_files = get_cache_files()
            if cache_files:
                for file_info in cache_files:
                    file_path = file_info.get('path')
                    if file_path and os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                            logging.info(f"Removed content source cache file: {file_path}")
                        except Exception as e:
                            logging.error(f"Failed to remove cache file {file_path}: {e}")
        except ImportError:
            logging.warning("Could not import get_cache_files from routes.debug_routes to clear cache.")
        except Exception as e:
            logging.error(f"An error occurred while clearing content source cache files: {e}")
        
        # Check if program was running before reinitialization
        was_program_running = False
        try:
            from routes.program_operation_routes import get_program_runner
            runner = get_program_runner()
            if runner and runner.is_running():
                was_program_running = True
                logging.info("Program was running before settings save. Will restart after reinitialization.")
        except Exception as e:
            logging.warning(f"Could not check program status before reinitialization: {e}")
        
        # Reinitialize components that depend on the config
        try:
            from debrid import reset_provider
            reset_provider()
            from queues.queue_manager import QueueManager
            QueueManager().reinitialize()
            from queues.run_program import ProgramRunner
            ProgramRunner().reinitialize()
            logging.info("Relevant components reinitialized.")
        except Exception as reinit_e:
             logging.error(f"Error during component reinitialization after settings save: {reinit_e}", exc_info=True)
             # Consider if the response should indicate partial success/failure
             # return jsonify({"status": "warning", "message": f"Settings updated but failed to reinitialize components: {reinit_e}"}), 500

        # Restart program if it was running before
        if was_program_running:
            try:
                from routes.program_operation_routes import _execute_start_program
                start_result = _execute_start_program(skip_connectivity_check=True, is_restart=True)
                if start_result.get("status") == "success":
                    logging.info("Program restarted successfully after settings save.")
                    return jsonify({"status": "success", "message": "Settings updated successfully and program restarted"})
                else:
                    logging.warning(f"Failed to restart program after settings save: {start_result.get('message', 'Unknown error')}")
                    return jsonify({"status": "warning", "message": f"Settings updated successfully but failed to restart program: {start_result.get('message', 'Unknown error')}"})
            except Exception as restart_e:
                logging.error(f"Error restarting program after settings save: {restart_e}", exc_info=True)
                return jsonify({"status": "warning", "message": f"Settings updated successfully but failed to restart program: {str(restart_e)}"})

        return jsonify({"status": "success", "message": "Settings updated successfully"})
    except Exception as e:
        logging.error(f"Error updating settings: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": f"An unexpected error occurred: {str(e)}"}), 500

@settings_bp.route('/api/reverse_parser_settings', methods=['GET'])
def get_reverse_parser_settings():
    config = load_config()
    reverse_parser_settings = config.get('Reverse Parser', {})
    
    # Get all scraping versions
    all_scraping_versions = set(config.get('Scraping', {}).get('versions', {}).keys())
    
    # Get the current version order, or initialize it if it doesn't exist
    version_order = reverse_parser_settings.get('version_order', [])
    
    # Ensure version_terms exists
    version_terms = reverse_parser_settings.get('version_terms', {})
    
    # Create a new ordered version_terms dictionary
    ordered_version_terms = {}
    
    # First, add versions in the order specified by version_order
    for version in version_order:
        if version in all_scraping_versions:
            ordered_version_terms[version] = version_terms.get(version, [])
            all_scraping_versions.remove(version)
    
    # Then, add any remaining versions that weren't in version_order
    for version in all_scraping_versions:
        ordered_version_terms[version] = version_terms.get(version, [])
    
    # Update version_order to include any new versions
    version_order = list(ordered_version_terms.keys())
    
    # Update the settings
    reverse_parser_settings['version_terms'] = ordered_version_terms
    reverse_parser_settings['version_order'] = version_order
    
    # Ensure default_version is set and valid
    if 'default_version' not in reverse_parser_settings or reverse_parser_settings['default_version'] not in ordered_version_terms:
        reverse_parser_settings['default_version'] = next(iter(ordered_version_terms), None)
    
    return jsonify(reverse_parser_settings)

def update_nested_settings(current, new):
    for key, value in new.items():
        if isinstance(value, dict):
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            if key == 'Content Sources':
                for source_id, source_config in value.items():
                    if source_id in current[key]:
                        update_content_source(source_id, source_config)
                    else:
                        add_content_source(source_config['type'], source_config)
            else:
                update_nested_settings(current[key], value)
        else:
            current[key] = value

@settings_bp.route('/versions/add', methods=['POST'])
@admin_required
def add_version():
    data = request.json
    version_name = data.get('name')
    if not version_name:
        return jsonify({'success': False, 'error': 'No version name provided'}), 400

    config = load_config()
    if 'Scraping' not in config:
        config['Scraping'] = {}
    if 'versions' not in config['Scraping']:
        config['Scraping']['versions'] = {}

    if version_name in config['Scraping']['versions']:
        return jsonify({'success': False, 'error': 'Version already exists'}), 400

    # Add the new version with default settings
    config['Scraping']['versions'][version_name] = {
        'enable_hdr': False,
        'max_resolution': '1080p',
        'resolution_wanted': '<=',
        'resolution_weight': 3,
        'hdr_weight': 3,
        'similarity_weight': 3,
        'size_weight': 3,
        'bitrate_weight': 3,
        'preferred_filter_in': [],
        'preferred_filter_out': [],
        'filter_in': [],
        'filter_out': [],
        'min_size_gb': 0.01,
        'max_size_gb': '',
        'wake_count': None,
        'require_physical_release': False  # Add default require_physical_release setting
    }

    save_config(config)
    return jsonify({'success': True, 'version_id': version_name})

@settings_bp.route('/versions/delete', methods=['POST'])
@admin_required
def delete_version():
    data = request.json
    version_id = data.get('version_id')
    target_version_id = data.get('target_version_id') # New: Can be None
    
    if not version_id:
        return jsonify({'success': False, 'error': 'No version ID provided'}), 400

    try:
        # Perform database update first
        from database import get_db_connection
        from database.database_writing import update_version_for_items
        
        conn = get_db_connection()
        updated_count = 0 # Initialize count
        try:
            updated_count = update_version_for_items(version_id, target_version_id)
            logging.info(f"Updated {updated_count} media items in database from version '{version_id}' to '{target_version_id or 'None'}'")
            conn.commit() # Commit the database change
        except Exception as db_error:
            conn.rollback()
            logging.error(f"Database error during version deletion: {db_error}", exc_info=True)
            return jsonify({'success': False, 'error': f'Database error: {db_error}'}), 500
        finally:
            conn.close()

        # Now update the config file
        config = load_config()
        if 'Scraping' in config and 'versions' in config['Scraping'] and version_id in config['Scraping']['versions']:
            # Before deleting, update fallbacks pointing to this version
            versions = config['Scraping']['versions']
            for v_name, v_config in versions.items():
                if v_name != version_id and v_config.get('fallback_version') == version_id:
                    versions[v_name]['fallback_version'] = 'None' # Set to None as requested
                    logging.info(f"Reset fallback_version for version '{v_name}' as '{version_id}' was deleted.")
            
            # --- New Logic: Remove deleted version from Content Sources ---
            content_sources = config.get('Content Sources', {})
            updated_sources_count = 0
            for source_id, source_config in content_sources.items():
                if isinstance(source_config, dict) and 'versions' in source_config:
                    source_versions = source_config['versions']
                    if isinstance(source_versions, dict) and version_id in source_versions:
                        del source_versions[version_id]
                        updated_sources_count += 1
                        logging.info(f"Removed deleted version '{version_id}' from Content Source '{source_id}' (dict format)")
                    elif isinstance(source_versions, list) and version_id in source_versions:
                        source_versions.remove(version_id)
                        updated_sources_count += 1
                        logging.info(f"Removed deleted version '{version_id}' from Content Source '{source_id}' (list format)")
            if updated_sources_count > 0:
                logging.info(f"Removed deleted version from {updated_sources_count} Content Source(s).")

            # Delete the actual version
            del versions[version_id]
            save_config(config) # Save config with updated fallbacks and deleted version
            
            message = f"Version '{version_id}' deleted."
            if updated_count > 0:
                message += f" {updated_count} items reassigned to '{target_version_id or 'versionless'}'."
            return jsonify({'success': True, 'message': message})
        else:
            # This case should ideally not happen if DB update succeeded, but handle defensively
            logging.warning(f"Version '{version_id}' was updated in DB but not found in config for deletion.")
            return jsonify({'success': False, 'error': 'Version not found in config after DB update'}), 404
            
    except Exception as e:
        logging.error(f"Error deleting version '{version_id}': {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/import_defaults', methods=['POST'])
@admin_required
def import_default_versions():
    default_versions = None
    try:
        # Determine the base path depending on whether the app is frozen
        if getattr(sys, 'frozen', False):
            # If the application is run as a bundle/frozen executable (e.g., PyInstaller)
            base_path = os.path.dirname(sys.executable)
        else:
            # If running as a normal script
            # Use __file__ to get the directory of the current script (settings_routes.py)
            # Then go up one level to get the project root
            base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) 

        default_versions_path = os.path.join(base_path, 'optional_default_versions.json')
        
        try:
            # Read the default versions from the JSON file using the absolute path
            with open(default_versions_path, 'r') as f:
                default_versions = json.load(f)
            logging.info(f"Successfully loaded default versions from: {default_versions_path}")

        except FileNotFoundError:
            logging.warning(f"Default versions file not found at path: {default_versions_path}. Using hardcoded fallback.")
            default_versions = HARDCODED_DEFAULT_VERSIONS
        except json.JSONDecodeError:
            logging.error(f"Invalid JSON in default versions file at path: {default_versions_path}. Using hardcoded fallback.")
            default_versions = HARDCODED_DEFAULT_VERSIONS
        
        if not default_versions or not isinstance(default_versions, dict) or 'versions' not in default_versions:
             # This case handles if the hardcoded version is somehow invalid or the file loaded empty/wrong format
             logging.error("Invalid default versions data (either from file or hardcoded). Cannot import.")
             return jsonify({'success': False, 'error': 'Invalid default versions data format'}), 400
            
        # Load current config
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
            
        # Add each default version with a unique name
        imported_count = 0
        for version_name, version_config in default_versions['versions'].items():
            base_name = version_name
            counter = 1
            new_name = base_name
            
            # Find a unique name for this version
            while new_name in config['Scraping']['versions']:
                new_name = f"{base_name} {counter}"
                counter += 1
                
            config['Scraping']['versions'][new_name] = version_config
            imported_count += 1
            logging.info(f"Imported default version '{version_name}' as '{new_name}'.")
        
        # Save the updated config
        if imported_count > 0:
            save_config(config)
            message = f"Successfully imported {imported_count} default version(s)."
        else:
            message = "No new default versions to import."

        return jsonify({'success': True, 'message': message})
        
    except Exception as e:
        logging.error(f"Unexpected error importing default versions: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f'Unexpected error: {str(e)}'}), 500

@settings_bp.route('/versions/rename', methods=['POST'])
@admin_required
def rename_version():
    data = request.json
    old_name = data.get('old_name')
    new_name = data.get('new_name')
    
    if not old_name or not new_name:
        return jsonify({'success': False, 'error': 'Missing old_name or new_name'}), 400

    config = load_config()
    if 'Scraping' in config and 'versions' in config['Scraping']:
        versions = config['Scraping']['versions']
        if old_name in versions:
            # Update version name in config
            versions[new_name] = versions.pop(old_name)
            # Don't save yet, more updates needed

            # Update version name in database
            updated_db_count = update_version_name(old_name, new_name)
            logging.info(f"Updated {updated_db_count} media items in database from version prefix '{old_name}' to '{new_name}'")

            # Update fallback_version references in other versions
            for v_name, v_config in versions.items():
                if v_name != new_name and v_config.get('fallback_version') == old_name:
                    versions[v_name]['fallback_version'] = new_name
                    logging.info(f"Updated fallback_version for version '{v_name}' from '{old_name}' to '{new_name}'")

            # --- New Logic: Update Content Source Version Assignments ---
            content_sources = config.get('Content Sources', {})
            updated_sources_count = 0
            for source_id, source_config in content_sources.items():
                if isinstance(source_config, dict) and 'versions' in source_config:
                    source_versions = source_config['versions']
                    
                    # Handle dictionary format {version_name: enabled_boolean}
                    if isinstance(source_versions, dict):
                        if old_name in source_versions:
                            enabled_status = source_versions.pop(old_name) # Remove old, get status
                            source_versions[new_name] = enabled_status # Add new with same status
                            updated_sources_count += 1
                            logging.info(f"Updated version assignment for Content Source '{source_id}': '{old_name}' -> '{new_name}' (dict format)")
                    
                    # Handle list format [version_name, ...] - Less common now but good to support
                    elif isinstance(source_versions, list):
                         if old_name in source_versions:
                             source_versions.remove(old_name)
                             if new_name not in source_versions: # Avoid duplicates
                                 source_versions.append(new_name)
                             updated_sources_count += 1
                             logging.info(f"Updated version assignment for Content Source '{source_id}': '{old_name}' -> '{new_name}' (list format)")
                             
            if updated_sources_count > 0:
                 logging.info(f"Updated version assignments in {updated_sources_count} Content Source(s).")
            # --- End New Logic ---

            # Save config with all updates (version rename, fallbacks, content sources)
            save_config(config)

            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Version not found'}), 404
    else:
        # Handle case where Scraping or versions section is missing
        return jsonify({'success': False, 'error': 'Scraping versions configuration not found'}), 404

@settings_bp.route('/versions/duplicate', methods=['POST'])
@admin_required
def duplicate_version():
    data = request.json
    version_id = data.get('version_id')
    
    if not version_id:
        return jsonify({'success': False, 'error': 'No version ID provided'}), 400

    config = load_config()
    if 'Scraping' not in config or 'versions' not in config['Scraping'] or version_id not in config['Scraping']['versions']:
        return jsonify({'success': False, 'error': 'Version not found'}), 404

    new_version_id = f"{version_id} Copy"
    counter = 1
    while new_version_id in config['Scraping']['versions']:
        new_version_id = f"{version_id} Copy {counter}"
        counter += 1

    # Create a deep copy of the version settings
    original_settings = config['Scraping']['versions'][version_id]
    new_settings = original_settings.copy()
    
    # Ensure require_physical_release is included in the copy
    if 'require_physical_release' not in new_settings:
        new_settings['require_physical_release'] = False

    config['Scraping']['versions'][new_version_id] = new_settings
    config['Scraping']['versions'][new_version_id]['display_name'] = new_version_id

    save_config(config)
    return jsonify({'success': True, 'new_version_id': new_version_id})

@settings_bp.route('/scraping/content')
def scraping_content():
    config = load_config() # Initial load
    # Add logging to see the config state within the route
    logging.debug(f"[scraping_content] Loaded config: {config}") 
    schema = SETTINGS_SCHEMA
    # Explicitly reload config right before accessing versions
    config = load_config() 
    version_names = list(config.get('Scraping', {}).get('versions', {}).keys())
    logging.debug(f"[scraping_content] Extracted version names: {version_names}") # Log extracted names
    return render_template('settings_tabs/scraping.html', 
                           settings=config, 
                           settings_schema=schema, 
                           version_names=version_names)

@settings_bp.route('/get_scraping_versions', methods=['GET'])
def get_scraping_versions_route():
    try:
        config = load_config()
        versions = config.get('Scraping', {}).get('versions', {}).keys()
        return jsonify({'versions': list(versions)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@settings_bp.route('/get_content_source_settings', methods=['GET'])
def get_content_source_settings_route():
    try:
        content_source_settings = get_content_source_settings()
        return jsonify(content_source_settings)
    except Exception as e:
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500

@settings_bp.route('/get_scraping_versions', methods=['GET'])
def get_scraping_versions():
    try:
        config = load_config()
        versions = config.get('Scraping', {}).get('versions', {}).keys()
        return jsonify({'versions': list(versions)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@settings_bp.route('/get_version_settings')
def get_version_settings_route():
    try:
        version = request.args.get('version')
        if not version:
            return jsonify({'error': 'No version provided'}), 400
        
        version_settings = get_version_settings(version)
        if not version_settings:
            return jsonify({'error': f'No settings found for version: {version}'}), 404
        
        # Ensure max_resolution is included in the settings
        if 'max_resolution' not in version_settings:
            version_settings['max_resolution'] = '1080p'  # or whatever the default should be
        
        return jsonify({version: version_settings})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@settings_bp.route('/save_version_settings', methods=['POST'])
@admin_required
def save_version_settings():
    data = request.json
    version = data.get('version')
    settings = data.get('settings')

    if not version or not settings:
        return jsonify({'success': False, 'error': 'Invalid data provided'}), 400

    try:
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
        
        # Handle wake_count conversion
        if 'wake_count' in settings:
            if settings['wake_count'] == '' or settings['wake_count'] == 'None' or settings['wake_count'] is None:
                settings['wake_count'] = None
            else:
                try:
                    settings['wake_count'] = int(settings['wake_count'])
                except (ValueError, TypeError):
                    settings['wake_count'] = None
        
        config['Scraping']['versions'][version] = settings
        save_config(config)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def update_required_settings(form_data):
    config = load_config()
    config['Plex']['url'] = form_data.get('plex_url')
    config['Plex']['token'] = form_data.get('plex_token')
    config['Plex']['shows_libraries'] = form_data.get('shows_libraries')
    config['Plex']['movies_libraries'] = form_data.get('movies_libraries')
    config['RealDebrid']['api_key'] = form_data.get('realdebrid_api_key')
    config['Metadata Battery']['url'] = form_data.get('metadata_battery_url')
    save_config(config)

@settings_bp.route('/notifications/enabled', methods=['GET'])
def get_enabled_notifications():
    try:
        config = load_config()
        notifications = config.get('Notifications', {})
        
        enabled_notifications = {}
        for notification_id, notification_config in notifications.items():
            # Ensure defaults are present
            notification_config = ensure_notification_defaults(notification_config)
            
            if notification_config.get('enabled', False):
                # Only include notifications that are enabled and have non-empty required fields
                if notification_config['type'] == 'Discord':
                    if notification_config.get('webhook_url'):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'Email':
                    # Only check required fields (smtp_username and smtp_password are optional)
                    if all([
                        notification_config.get('smtp_server'),
                        notification_config.get('smtp_port'),
                        notification_config.get('from_address'),
                        notification_config.get('to_address')
                    ]):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'Telegram':
                    if all([
                        notification_config.get('bot_token'),
                        notification_config.get('chat_id')
                    ]):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'NTFY':
                    if all([
                        notification_config.get('host'),
                        notification_config.get('topic')
                    ]):
                        enabled_notifications[notification_id] = notification_config
        
        return jsonify({
            'success': True,
            'enabled_notifications': enabled_notifications
        })
    except Exception as e:
        logging.error(f"Error getting enabled notifications: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/notifications/enabled_for_category/<category>', methods=['GET'])
def get_enabled_notifications_for_category(category):
    try:
        config = load_config()
        notifications = config.get('Notifications', {})
        
        enabled_notifications = {}
        for notification_id, notification_config in notifications.items():
            # Ensure defaults are present
            notification_config = ensure_notification_defaults(notification_config)
            
            if notification_config.get('enabled', False):
                # Check if the notification is enabled for this category
                notify_on = notification_config.get('notify_on', {})
                if not notify_on.get(category, False):
                    continue

                # Only include notifications that are enabled and have non-empty required fields
                if notification_config['type'] == 'Discord':
                    if notification_config.get('webhook_url'):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'Email':
                    # Only check required fields (smtp_username and smtp_password are optional)
                    if all([
                        notification_config.get('smtp_server'),
                        notification_config.get('smtp_port'),
                        notification_config.get('from_address'),
                        notification_config.get('to_address')
                    ]):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'Telegram':
                    if all([
                        notification_config.get('bot_token'),
                        notification_config.get('chat_id')
                    ]):
                        enabled_notifications[notification_id] = notification_config
                elif notification_config['type'] == 'NTFY':
                    if all([
                        notification_config.get('host'),
                        notification_config.get('topic')
                    ]):
                        enabled_notifications[notification_id] = notification_config

        return jsonify({
            'success': True,
            'enabled_notifications': enabled_notifications
        })
    except Exception as e:
        logging.error(f"Error getting enabled notifications for category {category}: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@settings_bp.route('/notifications/update_defaults', methods=['POST'])
@admin_required
def update_notification_defaults():
    try:
        config = load_config()
        if 'Notifications' not in config:
            config['Notifications'] = {}

        # Force update all notifications with proper defaults
        for notification_id, notification_config in config['Notifications'].items():
            if notification_config is not None:
                # Remove empty notify_on if it exists
                if 'notify_on' in notification_config and not notification_config['notify_on']:
                    del notification_config['notify_on']
                
                # Apply defaults
                notification_config = ensure_notification_defaults(notification_config)
                config['Notifications'][notification_id] = notification_config

        save_config(config)
        return jsonify({'success': True, 'message': 'Notification defaults updated successfully'})
    except Exception as e:
        logging.error(f"Error updating notification defaults: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/add_default', methods=['POST'])
@admin_required
def add_default_version():
    try:
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}

        # Get the default version settings from the schema
        version_schema = SETTINGS_SCHEMA['Scraping']['versions']['schema']
        default_version = {
            'enable_hdr': version_schema['enable_hdr']['default'],
            'max_resolution': version_schema['max_resolution']['default'],
            'resolution_wanted': version_schema['resolution_wanted']['default'],
            'resolution_weight': version_schema['resolution_weight']['default'],
            'hdr_weight': version_schema['hdr_weight']['default'],
            'similarity_weight': version_schema['similarity_weight']['default'],
            'similarity_threshold': version_schema['similarity_threshold']['default'],
            'similarity_threshold_anime': version_schema['similarity_threshold_anime']['default'],
            'size_weight': version_schema['size_weight']['default'],
            'bitrate_weight': version_schema['bitrate_weight']['default'],
            'preferred_filter_in': version_schema['preferred_filter_in']['default'],
            'preferred_filter_out': version_schema['preferred_filter_out']['default'],
            'filter_in': version_schema['filter_in']['default'],
            'filter_out': version_schema['filter_out']['default'],
            'min_size_gb': version_schema['min_size_gb']['default'],
            'max_size_gb': version_schema['max_size_gb']['default'],
            'min_bitrate_mbps': version_schema['min_bitrate_mbps']['default'],
            'max_bitrate_mbps': version_schema['max_bitrate_mbps']['default'],
            'wake_count': version_schema['wake_count']['default'],
            'require_physical_release': version_schema['require_physical_release']['default']
        }

        # Add the default version while preserving existing versions
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
        
        # Find a unique name for the default version
        version_name = 'Default'
        counter = 1
        while version_name in config['Scraping']['versions']:
            version_name = f'Default {counter}'
            counter += 1
            
        config['Scraping']['versions'][version_name] = default_version
        save_config(config)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/add_separate_versions', methods=['POST'])
@admin_required
def add_separate_versions():
    try:
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}

        # Get the default version settings from the schema
        version_schema = SETTINGS_SCHEMA['Scraping']['versions']['schema']
        base_version = {
            'resolution_wanted': version_schema['resolution_wanted']['default'],
            'resolution_weight': version_schema['resolution_weight']['default'],
            'hdr_weight': version_schema['hdr_weight']['default'],
            'similarity_weight': version_schema['similarity_weight']['default'],
            'similarity_threshold': version_schema['similarity_threshold']['default'],
            'similarity_threshold_anime': version_schema['similarity_threshold_anime']['default'],
            'size_weight': version_schema['size_weight']['default'],
            'bitrate_weight': version_schema['bitrate_weight']['default'],
            'preferred_filter_in': [],
            'preferred_filter_out': [],
            'filter_in': [],
            'filter_out': [],
            'min_size_gb': version_schema['min_size_gb']['default'],
            'max_size_gb': version_schema['max_size_gb']['default'],
            'min_bitrate_mbps': version_schema['min_bitrate_mbps']['default'],
            'max_bitrate_mbps': version_schema['max_bitrate_mbps']['default'],
            'wake_count': version_schema['wake_count']['default'],
            'require_physical_release': version_schema['require_physical_release']['default']
        }

        # Create 1080p version
        version_1080p = base_version.copy()
        version_1080p.update({
            'enable_hdr': False,
            'max_resolution': '1080p',
            'resolution_wanted': '<=', # Explicitly set to <= for 1080p
            'preferred_filter_in': [
                [
                    'REMUX',
                    100
                ],
                [
                    'WebDL',
                    50
                ],
                [
                    'Web-DL',
                    50
                ]
            ],
            'preferred_filter_out': [
                [
                    '720p',
                    5
                ],
                [
                    'TrueHD',
                    3
                ],
                [
                    'SDR',
                    5
                ]
            ],
            'filter_out': [
                'Telesync',
                '3D',
                '(?i)\\bHDTS\\b',
                'HD-TS',
                '\\.TS\\.',
                '\\.CAM\\.',
                'HDCAM',
                'Telecine',
                '(?i).*\\bTS\\b$'
            ]
        })

        # Create 4K version
        version_4k = base_version.copy()
        version_4k.update({
            'enable_hdr': True,
            'max_resolution': '2160p',
            'resolution_wanted': '==', # Keep 4K as ==
            'wake_count': 6,
            'preferred_filter_in': [
                [
                    'REMUX',
                    100
                ],
                [
                    'WebDL',
                    50
                ],
                [
                    'Web-DL',
                    50
                ]
            ],
            'preferred_filter_out': [
                [
                    '720p',
                    5
                ],
                [
                    'TrueHD',
                    3
                ],
                [
                    'SDR',
                    5
                ]
            ],
            'filter_out': [
                'Telesync',
                '3D',
                '(?i)\\bHDTS\\b',
                'HD-TS',
                '\\.TS\\.',
                '\\.CAM\\.',
                'HDCAM',
                'Telecine',
                '(?i).*\\bTS\\b$'
            ]
        })

        # Add the new versions while preserving existing versions
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
        
        # Find unique names for the versions
        version_1080p_name = '1080p'
        version_4k_name = '2160p'
        counter_1080p = 1
        counter_4k = 1
        
        while version_1080p_name in config['Scraping']['versions']:
            version_1080p_name = f'1080p {counter_1080p}'
            counter_1080p += 1
            
        while version_4k_name in config['Scraping']['versions']:
            version_4k_name = f'2160p {counter_4k}'
            counter_4k += 1
            
        # Add the new versions
        config['Scraping']['versions'][version_1080p_name] = version_1080p
        config['Scraping']['versions'][version_4k_name] = version_4k
        save_config(config)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/clear_all', methods=['POST'])
@admin_required
def clear_all_versions():
    try:
        config = load_config()
        if 'Scraping' in config:
            config['Scraping']['versions'] = {}
            save_config(config)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error clearing versions: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/notifications/test', methods=['POST'])
@admin_required
def test_notification():
    try:
        notification_id = request.json.get('notification_id')
        if not notification_id:
            return jsonify({'success': False, 'error': 'No notification ID provided'}), 400

        config = load_config()
        if 'Notifications' not in config or notification_id not in config['Notifications']:
            return jsonify({'success': False, 'error': 'Notification not found'}), 404

        notification_config = config['Notifications'][notification_id]
        
        # Create a test notification
        test_notification = {
            'title': 'Test Notification',
            'message': f'This is a test notification from {notification_config["title"]}',
            'type': 'info',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # Get the notification type
        notification_type = notification_config['type']
        
        # Send the test notification based on the type
        success = False
        message = "Test notification sent successfully"
        
        try:
            if notification_type == 'Telegram':
                if not notification_config.get('bot_token') or not notification_config.get('chat_id'):
                    return jsonify({'success': False, 'error': 'Missing Telegram configuration'}), 400
                
                content = f"<b>Test Notification</b>\n\nThis is a test message from CLI Debrid. If you're seeing this, your Telegram notifications are working correctly!"
                send_telegram_notification(
                    notification_config['bot_token'],
                    notification_config['chat_id'],
                    content
                )
                success = True
                
            elif notification_type == 'Discord':
                if not notification_config.get('webhook_url'):
                    return jsonify({'success': False, 'error': 'Missing Discord webhook URL'}), 400
                
                content = "**Test Notification**\n\nThis is a test message from CLI Debrid. If you're seeing this, your Discord notifications are working correctly!"
                send_discord_notification(
                    notification_config['webhook_url'],
                    content
                )
                success = True
                
            elif notification_type == 'NTFY':
                if not notification_config.get('host') or not notification_config.get('topic'):
                    return jsonify({'success': False, 'error': 'Missing NTFY configuration'}), 400
                
                content = "Test Notification\n\nThis is a test message from CLI Debrid. If you're seeing this, your NTFY notifications are working correctly!"
                send_ntfy_notification(
                    notification_config['host'],
                    notification_config.get('api_key', ''),
                    notification_config.get('priority', 'low'),
                    notification_config['topic'],
                    content
                )
                success = True
                
            elif notification_type == 'Email':
                # Only smtp_username and smtp_password are optional
                required_fields = ['smtp_server', 'smtp_port', 'from_address', 'to_address']
                missing_fields = [field for field in required_fields if not notification_config.get(field)]
                
                if missing_fields:
                    return jsonify({'success': False, 'error': f'Missing Email configuration: {", ".join(missing_fields)}'}), 400
                
                content = """
                <html>
                <body>
                <h2>Test Notification</h2>
                <p>This is a test message from CLI Debrid. If you're seeing this, your Email notifications are working correctly!</p>
                </body>
                </html>
                """
                
                smtp_config = {
                    'smtp_server': notification_config['smtp_server'],
                    'smtp_port': notification_config['smtp_port'],
                    'smtp_username': notification_config['smtp_username'],
                    'smtp_password': notification_config['smtp_password'],
                    'from_address': notification_config['from_address'],
                    'to_address': notification_config['to_address']
                }
                
                email_result = send_email_notification(smtp_config, content, 'test')
                if email_result:
                    success = True
                else:
                    # Email sending failed - check if it's likely due to authentication
                    if not notification_config.get('smtp_username') or not notification_config.get('smtp_password'):
                        return jsonify({'success': False, 'error': 'SMTP Authentication failed. This server requires authentication - please provide username and password.'}), 400
                    else:
                        return jsonify({'success': False, 'error': 'Email sending failed. Please check your SMTP configuration and credentials.'}), 400
            
            else:
                return jsonify({'success': False, 'error': f'Unknown notification type: {notification_type}'}), 400
                
            if success:
                logging.info(f"Test notification sent successfully for {notification_id}")
                return jsonify({'success': True, 'message': message})
            else:
                return jsonify({'success': False, 'error': 'Failed to send test notification'}), 500
                
        except Exception as e:
            logging.error(f"Error sending test notification: {str(e)}", exc_info=True)
            return jsonify({'success': False, 'error': f'Error sending test notification: {str(e)}'}), 500
            
    except Exception as e:
        logging.error(f"Error testing notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/settings_schema', methods=['GET'])
def api_settings_schema():
    try:
        return jsonify(SETTINGS_SCHEMA)
    except Exception as e:
        return jsonify({"error": "An error occurred while loading settings schema."}), 500

@settings_bp.route('/api/phalanx-disclaimer-status')
def get_phalanx_disclaimer_status():
    try:
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        disclaimer_file = os.path.join(db_content_dir, 'phalanx_disclaimer.json')
        if os.path.exists(disclaimer_file):
            with open(disclaimer_file, 'r') as f:
                status = json.load(f)
                # Only return True if they've actually made a choice (accepted or declined)
                return jsonify({'hasSeenDisclaimer': 'accepted' in status})
        return jsonify({'hasSeenDisclaimer': False})
    except Exception as e:
        logging.error(f"Error checking Phalanx disclaimer status: {str(e)}")
        return jsonify({'hasSeenDisclaimer': False})

@settings_bp.route('/api/phalanx-disclaimer-accept', methods=['POST'])
@admin_required
def accept_phalanx_disclaimer():
    try:
        data = request.json
        accepted = data.get('accepted', False)
        print(f"Accepted: {accepted}")
        
        # Ensure db_content directory exists
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        os.makedirs(db_content_dir, exist_ok=True)
        
        # Save the disclaimer status
        disclaimer_file = os.path.join(db_content_dir, 'phalanx_disclaimer.json')
        
        # Create the file with proper permissions
        with open(disclaimer_file, 'w', encoding='utf-8') as f:
            json.dump({
                'accepted': accepted,
                'timestamp': datetime.now().isoformat()
            }, f, indent=4)
        
        # Update the UI Settings based on user choice
        config = load_config()
        if 'UI Settings' not in config:
            config['UI Settings'] = {}
        config['UI Settings']['enable_phalanx_db'] = accepted
        save_config(config)
        
        logging.info(f"Phalanx disclaimer status saved: accepted={accepted}")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving Phalanx disclaimer status: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

# Support modal status endpoints
@settings_bp.route('/api/support-modal-status')
def get_support_modal_status():
    try:
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        status_file = os.path.join(db_content_dir, 'support_modal.json')
        if os.path.exists(status_file):
            with open(status_file, 'r') as f:
                status = json.load(f)
                # Return both seen status and page views
                return jsonify({
                    'hasSeenSupport': status.get('seen', False),
                    'pageViews': status.get('pageViews', 0)
                })
        return jsonify({
            'hasSeenSupport': False,
            'pageViews': 0
        })
    except Exception as e:
        logging.error(f"Error checking support modal status: {str(e)}")
        return jsonify({
            'hasSeenSupport': False,
            'pageViews': 0
        })

@settings_bp.route('/api/support-modal-seen', methods=['POST'])
@admin_required
def mark_support_modal_seen():
    try:
        # Ensure db_content directory exists
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        os.makedirs(db_content_dir, exist_ok=True)
        
        # Save the status
        status_file = os.path.join(db_content_dir, 'support_modal.json')
        
        # Create the file with proper permissions
        with open(status_file, 'w', encoding='utf-8') as f:
            json.dump({
                'seen': True,
                'pageViews': 0,  # Reset page views when marked as seen
                'timestamp': datetime.now().isoformat()
            }, f, indent=4)
        
        logging.info("Support modal marked as seen")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving support modal status: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/support-modal-pageview', methods=['POST'])
@admin_required
def increment_pageview():
    try:
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        os.makedirs(db_content_dir, exist_ok=True)
        status_file = os.path.join(db_content_dir, 'support_modal.json')
        
        # Load existing status or create new
        if os.path.exists(status_file):
            with open(status_file, 'r') as f:
                status = json.load(f)
        else:
            status = {
                'seen': False,
                'pageViews': 0,
                'timestamp': datetime.now().isoformat()
            }
        
        # Only increment if not already seen
        if not status.get('seen', False):
            status['pageViews'] = status.get('pageViews', 0) + 1
            
            # Save updated status
            with open(status_file, 'w', encoding='utf-8') as f:
                json.dump(status, f, indent=4)
        
        return jsonify({
            'success': True,
            'pageViews': status['pageViews'],
            'hasSeenSupport': status.get('seen', False)
        })
    except Exception as e:
        logging.error(f"Error incrementing page views: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/enabled_content_sources', methods=['GET'])
def get_enabled_content_sources_route():
    try:
        enabled_sources = get_enabled_content_sources()
        
        # Manually add content_requestor and overseerr_webhook to the list for UI purposes
        enabled_sources.append({
            'id': 'content_requestor',
            'type': 'Internal', # Assign a placeholder type
            'display_name': 'Content Requestor'
        })

        enabled_sources.append({
            'id': 'overseerr_webhook',
            'type': 'Internal', # Assign a placeholder type
            'display_name': 'Overseerr Webhook'
        })
        
        return jsonify({'success': True, 'sources': enabled_sources})
    except Exception as e:
        logging.error(f"Error getting enabled content sources: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/save_content_source_order', methods=['POST'])
@admin_required
def save_content_source_order():
    try:
        source_order_from_request = request.json.get('order', [])
        if not isinstance(source_order_from_request, list):
             return jsonify({'success': False, 'error': 'Invalid order format provided'}), 400

        # Filter out 'content_requestor' as it's not a real content source to be saved
        filtered_source_order = [source_id for source_id in source_order_from_request if source_id != 'content_requestor']
        
        if not filtered_source_order:
            logging.info("Content source priority order is empty after filtering.")
            # Decide if you want to save an empty string or handle differently
            # Saving empty string for now:
            order_string = ""
        else:
            # Join the filtered list into a comma-separated string
            order_string = ','.join(filtered_source_order)
        
        # Save to config
        config = load_config()
        if 'Queue' not in config:
            config['Queue'] = {}
        config['Queue']['content_source_priority'] = order_string
        save_config(config)
        
        logging.info(f"Saved filtered content source priority: {order_string}")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving content source order: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/versions/check_usage/<version_id>', methods=['GET'])
def check_version_usage(version_id):
    try:
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if any media items use this version
        cursor.execute("SELECT COUNT(*) FROM media_items WHERE version = ?", (version_id,))
        count = cursor.fetchone()[0]
        conn.close()

        in_use = count > 0
        is_last = False
        alternatives = []

        if in_use:
            # If in use, check if it's the last version and get alternatives
            config = load_config()
            all_versions = list(config.get('Scraping', {}).get('versions', {}).keys())
            alternatives = [v for v in all_versions if v != version_id]
            is_last = len(all_versions) <= 1

        return jsonify({
            'success': True,
            'in_use': in_use,
            'is_last': is_last,
            'alternatives': alternatives
        })

    except Exception as e:
        logging.error(f"Error checking version usage for {version_id}: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/trakt/import_liked_lists', methods=['POST'])
@admin_required
def get_liked_trakt_lists_for_import():
    """Fetches liked lists details from Trakt to be used by the frontend."""
    try:
        liked_lists = fetch_liked_trakt_lists_details()
        if not liked_lists:
            # Return success but indicate no lists found
            return jsonify({'success': True, 'lists': []}) 

        # Just return the fetched list details
        return jsonify({'success': True, 'lists': liked_lists})

    except Exception as e:
        logging.error(f"Error fetching liked Trakt lists details: {str(e)}", exc_info=True)
        # Return failure if there was an error during fetch
        return jsonify({'success': False, 'error': f'Failed to fetch liked lists details: {str(e)}'}), 500

@settings_bp.route('/api/mdblist-popup-status')
def get_mdblist_popup_status():
    try:
        # Check if we're in limited environment mode
        from utilities.set_supervisor_env import is_limited_environment
        limited_env = is_limited_environment()
        
        if not limited_env:
            return jsonify({'shouldShow': False})
        
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        status_file = os.path.join(db_content_dir, 'mdblist_popup.json')
        if os.path.exists(status_file):
            with open(status_file, 'r') as f:
                status = json.load(f)
                return jsonify({'shouldShow': not status.get('seen', False)})
        return jsonify({'shouldShow': True})
    except Exception as e:
        logging.error(f"Error checking MDBList popup status: {str(e)}")
        return jsonify({'shouldShow': False})

@settings_bp.route('/api/mdblist-popup-seen', methods=['POST'])
@admin_required
def mark_mdblist_popup_seen():
    try:
        # Ensure db_content directory exists
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        os.makedirs(db_content_dir, exist_ok=True)
        
        # Save the status
        status_file = os.path.join(db_content_dir, 'mdblist_popup.json')
        
        # Create the file with proper permissions
        with open(status_file, 'w', encoding='utf-8') as f:
            json.dump({
                'seen': True,
                'timestamp': datetime.now().isoformat()
            }, f, indent=4)
        
        logging.info("MDBList popup marked as seen")
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving MDBList popup status: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@settings_bp.route('/api/add-default-mdblists', methods=['POST'])
@admin_required
def add_default_mdblists():
    try:
        config = load_config()
        if 'Content Sources' not in config:
            config['Content Sources'] = {}
        
        # Check if MDBList sources already exist
        existing_mdblists = [source_id for source_id in config['Content Sources'].keys() 
                           if source_id.startswith('MDBList_')]
        
        if existing_mdblists:
            return jsonify({'success': False, 'error': 'MDBList sources already exist'}), 400
        
        # Get available versions from config
        available_versions = list(config.get('Scraping', {}).get('versions', {}).keys())
        if not available_versions:
            # If no versions configured, create a default 1080p version
            if 'Scraping' not in config:
                config['Scraping'] = {}
            if 'versions' not in config['Scraping']:
                config['Scraping']['versions'] = {}
            
            config['Scraping']['versions']['1080p'] = {
                'enable_hdr': False,
                'max_resolution': '1080p',
                'resolution_wanted': '<=',
                'resolution_weight': 3,
                'hdr_weight': 3,
                'similarity_weight': 3,
                'size_weight': 3,
                'bitrate_weight': 3,
                'preferred_filter_in': [],
                'preferred_filter_out': [],
                'filter_in': [],
                'filter_out': [],
                'min_size_gb': 0.01,
                'max_size_gb': None,
                'wake_count': None,
                'require_physical_release': False
            }
            available_versions = ['1080p']
            logging.info("Created default 1080p version for MDBLists")
        
        # Use the first available version (or 1080p as fallback)
        default_version = available_versions[0] if available_versions else '1080p'
        
        # Default MDBList configurations
        default_mdblists = {
            "MDBList_1": {
                "enabled": True,
                "urls": "https://mdblist.com/lists/hdlists/top-ten-pirated-movies-of-the-week-torrent-freak-com",
                "versions": {
                    default_version: True
                },
                "type": "MDBList",
                "media_type": "All",
                "display_name": "New Movies",
                "allow_specials": False,
                "custom_symlink_subfolder": "",
                "cutoff_date": "",
                "exclude_genres": []
            },
            "MDBList_2": {
                "enabled": True,
                "urls": "https://mdblist.com/lists/godver3/top-10-shows",
                "versions": {
                    default_version: True
                },
                "type": "MDBList",
                "media_type": "All",
                "display_name": "New Shows",
                "allow_specials": False,
                "custom_symlink_subfolder": "",
                "cutoff_date": "",
                "exclude_genres": []
            }
        }
        
        # Add the default MDBLists to the config
        for source_id, source_config in default_mdblists.items():
            config['Content Sources'][source_id] = source_config
        
        save_config(config)
        
        logging.info(f"Default MDBList sources added successfully using version: {default_version}")
        return jsonify({'success': True, 'message': f'Default MDBList sources added successfully using version: {default_version}'})
    except Exception as e:
        logging.error(f"Error adding default MDBLists: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
