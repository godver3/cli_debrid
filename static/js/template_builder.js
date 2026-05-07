/**
 * Template Builder JavaScript
 *
 * Handles drag-and-drop template creation, property editing, and preview.
 */

// Global state
let elements = [];
let selectedElement = null;
let draggedElement = null;
let isDragging = false;
let samplePosterLoaded = false;

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    initializeDragAndDrop();
    initializePropertyBindings();
    loadSamplePoster();
});

// ============================================
// Drag and Drop
// ============================================

function initializeDragAndDrop() {
    const paletteItems = document.querySelectorAll('.palette-item');
    const dropZone = document.getElementById('canvas-wrapper');

    paletteItems.forEach(item => {
        item.addEventListener('dragstart', (e) => {
            const elementType = item.dataset.elementType;
            const preset = item.dataset.preset;
            e.dataTransfer.setData('elementType', elementType);
            e.dataTransfer.setData('preset', preset || '');
        });
    });

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        const elementType = e.dataTransfer.getData('elementType');
        const preset = e.dataTransfer.getData('preset');

        if (elementType) {
            const rect = dropZone.getBoundingClientRect();
            const x = Math.max(0, e.clientX - rect.left);
            const y = Math.max(0, e.clientY - rect.top);

            addElement(elementType, x, y, preset);
        }
    });
}

// ============================================
// Element Management
// ============================================

function addElement(type, x, y, preset = '') {
    const element = {
        id: Date.now(),
        type: type,
        x: x || 30,
        y: y || 30,
        opacity: 1,
        position: 'custom'
    };

    if (type === 'text') {
        element.text = preset ? getPresetText(preset) : '{resolution}';
        element.font = 'DejaVuSans-Bold';
        element.size = 48;
        element.color = '#FFFFFF';
        element.background = preset ? '#000000CC' : '';
        element.condition = preset ? getPresetCondition(preset) : '';
    } else if (type === 'raster') {
        element.imagePath = '/resolution/2160p_hdr.png';
        element.width = null;
        element.height = null;
    }

    elements.push(element);
    updateElementsList();
    selectElement(element.id);
    generatePreview();
}

function getPresetText(preset) {
    const presets = {
        'resolution': '{resolution}',
        'hdr': '{hdr_format}',
        'audio': '{audio_codec}'
    };
    return presets[preset] || '{resolution}';
}

function getPresetCondition(preset) {
    const conditions = {
        'resolution': 'resolution IS NOT NULL',
        'hdr': 'hdr == true',
        'audio': 'audio_codec IS NOT NULL'
    };
    return conditions[preset] || '';
}

function selectElement(elementId) {
    selectedElement = elements.find(el => el.id === elementId);

    // Update UI
    document.querySelectorAll('.element-list-item').forEach(item => {
        item.classList.toggle('selected', parseInt(item.dataset.elementId) === elementId);
    });

    // Show properties panel
    document.getElementById('no-selection-message').style.display = 'none';
    document.getElementById('properties-content').style.display = 'block';
    document.getElementById('element-properties').style.display = 'block';

    // Populate properties
    if (selectedElement) {
        populateProperties(selectedElement);
    }
}

function deleteSelectedElement() {
    if (!selectedElement) return;

    showPopup({
        type: 'confirm',
        title: 'Delete Element',
        message: 'Delete this element?',
        confirmText: 'Confirm',
        cancelText: 'Cancel',
        onConfirm: function() {
            elements = elements.filter(el => el.id !== selectedElement.id);
            selectedElement = null;
            document.getElementById('element-properties').style.display = 'none';
            updateElementsList();
            generatePreview();
        }
    });
}

function updateElementsList() {
    const list = document.getElementById('elements-list');
    list.innerHTML = '';

    elements.forEach((el, index) => {
        const item = document.createElement('li');
        item.className = 'element-list-item';
        item.dataset.elementId = el.id;
        item.onclick = () => selectElement(el.id);

        const label = el.type === 'text' ? el.text : el.imagePath;
        item.innerHTML = `
            <span>${index + 1}. ${el.type.toUpperCase()}: ${label}</span>
            <div class="element-list-item-actions">
                <button class="btn btn-danger" onclick="deleteElement(${el.id}); event.stopPropagation();">
                    <i class="fas fa-trash"></i>
                </button>
            </div>
        `;
        list.appendChild(item);
    });

    if (elements.length === 0) {
        list.innerHTML = '<li style="text-align: center; color: #999; padding: 20px;">No elements yet<br><small>Drag elements from the palette</small></li>';
    }
}

function deleteElement(elementId) {
    elements = elements.filter(el => el.id !== elementId);
    if (selectedElement && selectedElement.id === elementId) {
        selectedElement = null;
        document.getElementById('element-properties').style.display = 'none';
    }
    updateElementsList();
    generatePreview();
}

// ============================================
// Property Bindings
// ============================================

function initializePropertyBindings() {
    // Position
    document.getElementById('prop-position').addEventListener('change', updateSelectedElementProperty);
    document.getElementById('prop-x').addEventListener('input', updateSelectedElementProperty);
    document.getElementById('prop-y').addEventListener('input', updateSelectedElementProperty);
    document.getElementById('prop-opacity').addEventListener('input', (e) => {
        document.getElementById('opacity-value').textContent = Math.round(e.target.value * 100) + '%';
        updateSelectedElementProperty();
    });

    // Text properties
    document.getElementById('prop-text').addEventListener('input', updateSelectedElementProperty);
    document.getElementById('prop-font').addEventListener('change', updateSelectedElementProperty);
    document.getElementById('prop-size').addEventListener('input', updateSelectedElementProperty);
    document.getElementById('prop-color').addEventListener('input', (e) => {
        document.getElementById('prop-color-hex').value = e.target.value;
        updateSelectedElementProperty();
    });
    document.getElementById('prop-color-hex').addEventListener('input', (e) => {
        document.getElementById('prop-color').value = e.target.value.substring(0, 7);
        updateSelectedElementProperty();
    });
    document.getElementById('prop-background').addEventListener('input', (e) => {
        document.getElementById('prop-background-hex').value = e.target.value + 'CC';
        updateSelectedElementProperty();
    });
    document.getElementById('prop-background-hex').addEventListener('input', updateSelectedElementProperty);
    document.getElementById('prop-has-background').addEventListener('change', updateSelectedElementProperty);

    // Raster properties
    document.getElementById('prop-image-path').addEventListener('input', updateSelectedElementProperty);
    document.getElementById('prop-width').addEventListener('input', updateSelectedElementProperty);
    document.getElementById('prop-height').addEventListener('input', updateSelectedElementProperty);

    // Condition
    document.getElementById('prop-condition-type').addEventListener('change', (e) => {
        document.getElementById('simple-condition').style.display = e.target.value === 'simple' ? 'block' : 'none';
        document.getElementById('custom-condition').style.display = e.target.value === 'custom' ? 'block' : 'none';
        updateSelectedElementProperty();
    });
    document.getElementById('cond-variable').addEventListener('change', updateSelectedElementProperty);
    document.getElementById('cond-operator').addEventListener('change', updateSelectedElementProperty);
    document.getElementById('cond-value').addEventListener('input', updateSelectedElementProperty);
    document.getElementById('prop-condition-custom').addEventListener('input', updateSelectedElementProperty);
}

function populateProperties(element) {
    // Common properties
    document.getElementById('prop-position').value = element.position || 'custom';
    document.getElementById('prop-x').value = element.x || 30;
    document.getElementById('prop-y').value = element.y || 30;
    document.getElementById('prop-opacity').value = element.opacity || 1;
    document.getElementById('opacity-value').textContent = Math.round((element.opacity || 1) * 100) + '%';

    // Show/hide type-specific properties
    document.getElementById('text-properties').style.display = element.type === 'text' ? 'block' : 'none';
    document.getElementById('raster-properties').style.display = element.type === 'raster' ? 'block' : 'none';

    if (element.type === 'text') {
        document.getElementById('prop-text').value = element.text || '';
        document.getElementById('prop-font').value = element.font || 'DejaVuSans-Bold';
        document.getElementById('prop-size').value = element.size || 48;
        document.getElementById('prop-color').value = (element.color || '#FFFFFF').substring(0, 7);
        document.getElementById('prop-color-hex').value = element.color || '#FFFFFF';

        const hasBackground = element.background && element.background !== '';
        document.getElementById('prop-has-background').checked = hasBackground;
        if (hasBackground) {
            document.getElementById('prop-background').value = element.background.substring(0, 7);
            document.getElementById('prop-background-hex').value = element.background;
        }
    } else if (element.type === 'raster') {
        document.getElementById('prop-image-path').value = element.imagePath || '';
        document.getElementById('prop-width').value = element.width || '';
        document.getElementById('prop-height').value = element.height || '';
    }

    // Condition
    if (element.condition) {
        document.getElementById('prop-condition-type').value = 'custom';
        document.getElementById('custom-condition').style.display = 'block';
        document.getElementById('prop-condition-custom').value = element.condition;
    } else {
        document.getElementById('prop-condition-type').value = '';
        document.getElementById('simple-condition').style.display = 'none';
        document.getElementById('custom-condition').style.display = 'none';
    }
}

function updateSelectedElementProperty() {
    if (!selectedElement) return;

    // Common properties
    selectedElement.position = document.getElementById('prop-position').value;
    selectedElement.x = parseInt(document.getElementById('prop-x').value) || 0;
    selectedElement.y = parseInt(document.getElementById('prop-y').value) || 0;
    selectedElement.opacity = parseFloat(document.getElementById('prop-opacity').value);

    if (selectedElement.type === 'text') {
        selectedElement.text = document.getElementById('prop-text').value;
        selectedElement.font = document.getElementById('prop-font').value;
        selectedElement.size = parseInt(document.getElementById('prop-size').value) || 48;
        selectedElement.color = document.getElementById('prop-color-hex').value;

        if (document.getElementById('prop-has-background').checked) {
            selectedElement.background = document.getElementById('prop-background-hex').value;
        } else {
            selectedElement.background = '';
        }
    } else if (selectedElement.type === 'raster') {
        selectedElement.imagePath = document.getElementById('prop-image-path').value;
        selectedElement.width = parseInt(document.getElementById('prop-width').value) || null;
        selectedElement.height = parseInt(document.getElementById('prop-height').value) || null;
    }

    // Condition
    const conditionType = document.getElementById('prop-condition-type').value;
    if (conditionType === 'simple') {
        const variable = document.getElementById('cond-variable').value;
        const operator = document.getElementById('cond-operator').value;
        const value = document.getElementById('cond-value').value;
        selectedElement.condition = `${variable} ${operator} '${value}'`;
    } else if (conditionType === 'custom') {
        selectedElement.condition = document.getElementById('prop-condition-custom').value;
    } else {
        selectedElement.condition = '';
    }

    updateElementsList();
    generatePreview();
}

// ============================================
// Preview Generation
// ============================================

function loadSamplePoster() {
    const canvas = document.getElementById('preview-canvas');
    const ctx = canvas.getContext('2d');

    // Draw a sample poster background
    const gradient = ctx.createLinearGradient(0, 0, 0, canvas.height);
    gradient.addColorStop(0, '#1a1a2e');
    gradient.addColorStop(1, '#16213e');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    // Add title
    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 48px Arial';
    ctx.textAlign = 'center';
    ctx.fillText('Sample Movie Poster', canvas.width / 2, canvas.height / 2);

    ctx.font = '24px Arial';
    ctx.fillText('Drag elements to add overlays', canvas.width / 2, canvas.height / 2 + 40);

    samplePosterLoaded = true;
}

function generatePreview() {
    const canvas = document.getElementById('preview-canvas');
    const ctx = canvas.getContext('2d');

    // Redraw base
    loadSamplePoster();

    // Sample media info for preview
    const sampleMedia = {
        resolution: '2160p',
        hdr: true,
        hdr_format: 'HDR10',
        audio_codec: 'TrueHD Atmos',
        video_codec: 'HEVC'
    };

    // Render each element
    elements.forEach(element => {
        if (element.type === 'text') {
            renderTextPreview(ctx, element, sampleMedia);
        } else if (element.type === 'raster') {
            // Note: Can't easily preview images in canvas without loading them
            renderRasterPlaceholder(ctx, element);
        }
    });
}

function renderTextPreview(ctx, element, mediaInfo) {
    // Interpolate text
    let text = element.text || '';
    text = text.replace('{resolution}', mediaInfo.resolution || '');
    text = text.replace('{hdr_format}', mediaInfo.hdr_format || '');
    text = text.replace('{audio_codec}', mediaInfo.audio_codec || '');
    text = text.replace('{video_codec}', mediaInfo.video_codec || '');

    if (!text) return;

    // Set font
    const fontSize = element.size || 48;
    ctx.font = `bold ${fontSize}px ${element.font || 'Arial'}`;

    // Measure text
    const metrics = ctx.measureText(text);
    const textWidth = metrics.width;
    const textHeight = fontSize;

    // Draw background if specified
    if (element.background) {
        ctx.fillStyle = element.background;
        const padding = 10;
        ctx.fillRect(
            element.x - padding,
            element.y - padding,
            textWidth + padding * 2,
            textHeight + padding
        );
    }

    // Draw text
    ctx.fillStyle = element.color || '#FFFFFF';
    ctx.globalAlpha = element.opacity || 1;
    ctx.fillText(text, element.x, element.y + fontSize - 10);
    ctx.globalAlpha = 1;
}

function renderRasterPlaceholder(ctx, element) {
    // Draw placeholder box for image
    ctx.strokeStyle = '#007bff';
    ctx.setLineDash([5, 5]);
    ctx.strokeRect(element.x, element.y, element.width || 100, element.height || 100);
    ctx.setLineDash([]);

    ctx.fillStyle = 'rgba(0, 123, 255, 0.1)';
    ctx.fillRect(element.x, element.y, element.width || 100, element.height || 100);

    ctx.fillStyle = '#007bff';
    ctx.font = '12px Arial';
    ctx.fillText('Image', element.x + 10, element.y + 20);
}

// ============================================
// Template Save/Load
// ============================================

function saveTemplate() {
    const name = document.getElementById('template-name').value.trim();
    const description = document.getElementById('template-description').value.trim();
    const mediaType = document.getElementById('template-media-type').value;
    const isActive = document.getElementById('template-active').checked;

    if (!name) {
        showPopup({ type: 'warning', title: 'Required', message: 'Please enter a template name', autoClose: 4000 });
        return;
    }

    if (elements.length === 0) {
        showPopup({ type: 'warning', title: 'Required', message: 'Please add at least one element', autoClose: 4000 });
        return;
    }

    // Build template JSON
    const templateData = {
        name: name,
        description: description,
        media_type: mediaType,
        elements: elements.map(el => {
            const elem = {
                type: el.type,
                position: el.position,
                x: el.x,
                y: el.y,
                opacity: el.opacity
            };

            if (el.condition) elem.condition = el.condition;

            if (el.type === 'text') {
                elem.text = el.text;
                elem.font = el.font;
                elem.size = el.size;
                elem.color = el.color;
                if (el.background) elem.background = el.background;
            } else if (el.type === 'raster') {
                elem.imagePath = el.imagePath;
                if (el.width) elem.width = el.width;
                if (el.height) elem.height = el.height;
            }

            return elem;
        })
    };

    // Save via API
    fetch('/api/overlays/templates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            name: name,
            description: description,
            media_type: mediaType,
            template_data: templateData,
            is_active: isActive
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showPopup({
                type: 'confirm',
                title: 'Success',
                message: 'Template saved successfully! Go to overlay management page?',
                confirmText: 'Confirm',
                cancelText: 'Cancel',
                onConfirm: function() { window.location.href = '/overlays'; }
            });
        } else {
            showPopup({ type: 'error', title: 'Error', message: 'Failed to save template: ' + data.error, autoClose: 4000 });
        }
    })
    .catch(error => {
        showPopup({ type: 'error', title: 'Error', message: 'Error saving template: ' + error, autoClose: 4000 });
    });
}

function clearCanvas() {
    showPopup({
        type: 'confirm',
        title: 'Clear Canvas',
        message: 'Clear all elements?',
        confirmText: 'Confirm',
        cancelText: 'Cancel',
        onConfirm: function() {
            elements = [];
            selectedElement = null;
            updateElementsList();
            generatePreview();
            document.getElementById('element-properties').style.display = 'none';
        }
    });
}

function browseAssets() {
    // TODO: Implement asset browser modal
    showPopup({ type: 'info', title: 'Notice', message: 'Asset browser not yet implemented. Enter path manually (e.g., /resolution/2160p_hdr.png)', autoClose: 4000 });
}
