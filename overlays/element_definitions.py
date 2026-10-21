"""
Overlay Element Definitions

Pre-configured element types that users can add to their layouts.
Each element knows what data to fetch and how to display it.
"""

ELEMENT_DEFINITIONS = {
    # BASIC ELEMENTS
    'basic': {
        'text': {
            'name': 'Text',
            'icon': 'A',
            'type': 'text',
            'description': 'Static or dynamic text label',
            'defaults': {
                'content': 'Text',
                'font_size': 24,
                'font_color': '#FFFFFF',
                'font_family': 'Arial',
                'x': 50,
                'y': 50
            }
        },
        'variable_text': {
            'name': 'Variable Text',
            'icon': '</>',
            'type': 'variable_text',
            'description': 'Dynamic text from media metadata',
            'defaults': {
                'variable': 'title',  # title, year, runtime, etc.
                'font_size': 24,
                'font_color': '#FFFFFF',
                'font_family': 'Arial',
                'x': 50,
                'y': 50
            }
        },
        'image': {
            'name': 'Image (PNG/JPG)',
            'icon': '🖼',
            'type': 'image',
            'description': 'Custom image or icon',
            'defaults': {
                'image_path': '',
                'width': 100,
                'height': 100,
                'x': 50,
                'y': 50
            }
        },
        'svg_vector': {
            'name': 'SVG Vector',
            'icon': '🔷',
            'type': 'svg',
            'description': 'Scalable vector graphic',
            'defaults': {
                'svg_path': '',
                'width': 100,
                'height': 100,
                'x': 50,
                'y': 50,
                'fill_color': '#FFFFFF'
            }
        },
        'tile_shape': {
            'name': 'Tile/Shape',
            'icon': '■',
            'type': 'shape',
            'description': 'Rectangle, circle, or other shape',
            'defaults': {
                'shape_type': 'rectangle',  # rectangle, circle, rounded_rect
                'width': 100,
                'height': 100,
                'x': 50,
                'y': 50,
                'fill_color': '#000000',
                'border_color': '#FFFFFF',
                'border_width': 2,
                'opacity': 0.8
            }
        }
    },

    # RATING BADGES
    'rating_badges': {
        'imdb_rating': {
            'name': 'IMDb Rating',
            'icon': '⭐',
            'type': 'rating',
            'description': 'IMDb rating badge',
            'defaults': {
                'source': 'imdb',
                'show_icon': True,
                'show_score': True,
                'font_size': 20,
                'x': 20,
                'y': 20,
                'width': 80,
                'height': 40
            }
        },
        'tmdb_rating': {
            'name': 'TMDb Rating',
            'icon': '🎬',
            'type': 'rating',
            'description': 'The Movie Database rating',
            'defaults': {
                'source': 'tmdb',
                'show_icon': True,
                'show_score': True,
                'font_size': 20,
                'x': 20,
                'y': 20,
                'width': 80,
                'height': 40
            }
        },
        'trakt_rating': {
            'name': 'Trakt Rating',
            'icon': '❤',
            'type': 'rating',
            'description': 'Trakt rating badge',
            'defaults': {
                'source': 'trakt',
                'show_icon': True,
                'show_score': True,
                'font_size': 20,
                'x': 20,
                'y': 20,
                'width': 80,
                'height': 40
            }
        },
        'rotten_tomatoes': {
            'name': 'Rotten Tomatoes',
            'icon': '🍅',
            'type': 'rating',
            'description': 'Rotten Tomatoes score',
            'defaults': {
                'source': 'rotten_tomatoes',
                'show_icon': True,
                'show_score': True,
                'font_size': 20,
                'x': 20,
                'y': 20,
                'width': 80,
                'height': 40
            }
        }
    },

    # QUICK BADGES
    'quick_badges': {
        'resolution': {
            'name': 'Resolution',
            'icon': '📺',
            'type': 'badge',
            'description': 'Resolution badge (4K, 1080p, etc.)',
            'defaults': {
                'badge_type': 'resolution',
                'show_text': True,
                'x': 20,
                'y': 20,
                'width': 60,
                'height': 30
            }
        },
        'hdr_format': {
            'name': 'HDR Format',
            'icon': '☀',
            'type': 'badge',
            'description': 'HDR, Dolby Vision, HDR10+',
            'defaults': {
                'badge_type': 'hdr',
                'show_text': True,
                'x': 90,
                'y': 20,
                'width': 60,
                'height': 30
            }
        },
        'audio_codec': {
            'name': 'Audio Codec',
            'icon': '🔊',
            'type': 'badge',
            'description': 'Audio codec badge (Atmos, DTS-X, etc.)',
            'defaults': {
                'badge_type': 'audio',
                'show_text': True,
                'x': 160,
                'y': 20,
                'width': 80,
                'height': 30
            }
        },
        'format': {
            'name': 'Format',
            'icon': '💿',
            'type': 'badge',
            'description': 'Format badge (REMUX, WEB-DL, etc.)',
            'defaults': {
                'badge_type': 'format',
                'show_text': True,
                'x': 20,
                'y': 60,
                'width': 80,
                'height': 30
            }
        },
        'network': {
            'name': 'Network',
            'icon': '📡',
            'type': 'badge',
            'description': 'TV network logo (HBO, Netflix, etc.)',
            'defaults': {
                'badge_type': 'network',
                'show_logo': True,
                'x': 20,
                'y': 100,
                'width': 80,
                'height': 40
            }
        },
        'studio': {
            'name': 'Studio',
            'icon': '🎭',
            'type': 'badge',
            'description': 'Production studio logo',
            'defaults': {
                'badge_type': 'studio',
                'show_logo': True,
                'x': 20,
                'y': 150,
                'width': 80,
                'height': 40
            }
        }
    },

    # DESIGNED BADGES
    # Fully procedural two-segment badges with gradient backgrounds,
    # Google Fonts text, and independent opacity control for every layer.
    'designed_badges': {
        'designed_badge': {
            'name': 'Designed Badge',
            'icon': '✦',
            'type': 'designed_badge',
            'description': 'Fully custom two-segment badge — gradient bg, Google Fonts, per-layer opacity',
            'defaults': {
                'x': 20, 'y': 20,
                'width': 180, 'height': 36,
                'opacity': 1.0,
                'borderRadius': 8,
                'borderEnabled': True,
                'borderColor': '#ffffff', 'borderOpacity': 0.08, 'borderWidth': 1,
                'highlightEnabled': True, 'highlightOpacity': 0.09,
                'bgType': 'solid',
                'bgColor': '#ffffff', 'bgOpacity': 0.03,
                'bgColor2': '#ffffff', 'bgGradientAngle': 135,
                'leftEnabled': True,
                'leftWidth': 60, 'leftPaddingH': 10,
                'leftBgColor': '#000000', 'leftBgOpacity': 0.0,
                'leftText': '4K',
                'leftFont': 'Bebas Neue', 'leftFontSize': 18,
                'leftColor': '#ffffff', 'leftOpacity': 0.9,
                'leftBold': False,
                'dividerEnabled': True,
                'dividerColor': '#ffffff', 'dividerOpacity': 0.07,
                'rightEnabled': True,
                'rightPaddingH': 8,
                'rightBgType': 'gradient',
                'rightBgColor': '#7838ff', 'rightBgColor2': '#ff6e14',
                'rightBgOpacity': 0.15, 'rightBgGradientAngle': 135,
                'rightLayout': 'stacked',
                'rightText1': 'DV',
                'rightFont1': 'Barlow Condensed', 'rightFontSize1': 12,
                'rightColor1': '#bc94ff', 'rightOpacity1': 0.92, 'rightBold1': True,
                'rightText2': 'HDR10+',
                'rightFont2': 'Barlow Condensed', 'rightFontSize2': 10,
                'rightColor2': '#ffa848', 'rightOpacity2': 0.92, 'rightBold2': True,
            }
        }
    },

    # SMART BADGES
    # Library-based badges that auto-select the correct variation PNG
    # from the badge library based on the item's metadata at render time.
    'smart_badges': {
        'audio_codec_badge': {
            'name': 'Audio Codec Badge',
            'icon': '🔊',
            'type': 'smart_badge',
            'description': 'Auto-selects audio codec PNG from badge library (TrueHD Atmos, DTS:X, etc.)',
            'defaults': {
                'badge_type': 'audio_codec',
                'x': 20,
                'y': 20,
                'width': 140,
                'height': 44,
                'opacity': 1.0
            }
        },
        'audio_channels_badge': {
            'name': 'Audio Channels Badge',
            'icon': '📻',
            'type': 'smart_badge',
            'description': 'Auto-selects channel badge PNG from badge library (5.1, 7.1, etc.)',
            'defaults': {
                'badge_type': 'audio_channels',
                'x': 170,
                'y': 20,
                'width': 60,
                'height': 44,
                'opacity': 1.0
            }
        },
        'audio_combo_badge': {
            'name': 'Audio Codec + Channels Badge',
            'icon': '🎵',
            'type': 'smart_badge',
            'description': 'Combined codec+channels in one compact PNG (TrueHD Atmos 7.1, DTS:X 5.1, etc.)',
            'defaults': {
                'badge_type': 'audio_combo',
                'x': 20,
                'y': 20,
                'width': 180,
                'height': 44,
                'opacity': 1.0
            }
        },
        'resolution_badge': {
            'name': 'Resolution Badge',
            'icon': '📺',
            'type': 'smart_badge',
            'description': 'Auto-selects resolution PNG from badge library (4K, 1080p, etc.)',
            'defaults': {
                'badge_type': 'resolution',
                'x': 20,
                'y': 20,
                'width': 80,
                'height': 44,
                'opacity': 1.0
            }
        },
        'hdr_badge': {
            'name': 'HDR Format Badge',
            'icon': '☀',
            'type': 'smart_badge',
            'description': 'Auto-selects HDR/DV badge PNG from badge library (HDR, DV, HDR10+, etc.)',
            'defaults': {
                'badge_type': 'hdr',
                'x': 110,
                'y': 20,
                'width': 80,
                'height': 44,
                'opacity': 1.0
            }
        },
        'resolution_hdr_badge': {
            'name': 'Resolution + HDR Badge',
            'icon': '🎬',
            'type': 'smart_badge',
            'description': 'Combined resolution+HDR in one compact PNG (4K+HDR, 4K+DV, 1080p+HDR, etc.)',
            'defaults': {
                'badge_type': 'resolution_hdr',
                'x': 20,
                'y': 20,
                'width': 140,
                'height': 44,
                'opacity': 1.0
            }
        },
    }
}


def get_element_definition(category: str, element_id: str) -> dict:
    """Get definition for a specific element."""
    if category in ELEMENT_DEFINITIONS:
        return ELEMENT_DEFINITIONS[category].get(element_id)
    return None


def get_all_elements_by_category() -> dict:
    """Get all element definitions grouped by category."""
    return ELEMENT_DEFINITIONS


def create_element_instance(category: str, element_id: str, overrides: dict = None) -> dict:
    """
    Create an element instance with defaults merged with overrides.

    Args:
        category: Element category (basic, rating_badges, quick_badges)
        element_id: Specific element ID within the category
        overrides: Dictionary of properties to override defaults

    Returns:
        Element instance dictionary
    """
    definition = get_element_definition(category, element_id)
    if not definition:
        raise ValueError(f"Unknown element: {category}/{element_id}")

    instance = {
        'id': f"{element_id}_{category}",  # Unique instance ID
        'type': definition['type'],
        'name': definition['name'],
        **definition['defaults']
    }

    if overrides:
        instance.update(overrides)

    return instance
