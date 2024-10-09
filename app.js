const map = L.map('map').setView([62.0, 15.0], 5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors'
}).addTo(map);

const reportsList = document.getElementById('reportsList');
const loading = document.getElementById('loading');
const sidebar = document.getElementById('sidebar');
const toggleSidebar = document.getElementById('toggle-sidebar');
const filterOptions = document.getElementById('filter-options');
const resetFilters = document.getElementById('reset-filters');
const selectAllFilters = document.getElementById('select-all-filters');
const searchBar = document.getElementById('search-bar');
const filterInfo = document.getElementById('filter-info');

const markers = L.markerClusterGroup({
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    zoomToBoundsOnClick: true,
    spiderfyDistanceMultiplier: 2
});

const colorMap = new Map();
const baseColors = [
    '#FF0000', '#00FF00', '#0000FF', '#FFFF00', '#FF00FF', '#00FFFF',
    '#FFA500', '#800080', '#008000', '#FFC0CB', '#A52A2A', '#808080'
];

const emojiMap = {
    'Trafikolycka': '🚗💥',
    'Brand': '🔥',
    'Stöld': '🕵️',
    'Misshandel': '🤜',
    'Rattfylleri': '🍺🚗',
    'Narkotikabrott': '💊',
    'Skottlossning': '🔫',
    'Explosion': '💥',
    'Rån': '🦹',
    'Inbrott': '🏠🔨',
    'Försvunnen person': '🔍👤',
    'Vapenbrott': '🔪'
};

let allReports = new Map();
let activeFilters = new Set();
let searchTerm = '';

function getColorForType(type) {
    if (!colorMap.has(type)) {
        const color = baseColors[colorMap.size % baseColors.length];
        colorMap.set(type, color);
    }
    return colorMap.get(type);
}

function getEmojiForType(type) {
    return emojiMap[type] || '📋';
}

function applyColorToElement(element, type) {
    const color = getColorForType(type);
    element.style.backgroundColor = color;
    element.style.color = getContrastColor(color);
}

function getContrastColor(hexcolor) {
    const r = parseInt(hexcolor.substr(1,2), 16);
    const g = parseInt(hexcolor.substr(3,2), 16);
    const b = parseInt(hexcolor.substr(5,2), 16);
    const yiq = ((r * 299) + (g * 587) + (b * 114)) / 1000;
    return (yiq >= 128) ? 'black' : 'white';
}

function formatDate(dateString) {
    const date = new Date(dateString);
    return date.toLocaleString('sv-SE', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
    });
}

function createReportItem(event) {
    const reportItem = document.createElement('div');
    reportItem.className = 'report-item';
    reportItem.id = `report-${event.id}`;

    const typeSpan = document.createElement('span');
    typeSpan.className = 'crime-type';
    typeSpan.textContent = event.type;
    applyColorToElement(typeSpan, event.type);

    reportItem.innerHTML = `
        <h3 class="report-title">${event.name.split(',')[1]}, ${event.name.split(',')[2]}</h3>
        <div class="report-details">
            <span class="report-emoji">${getEmojiForType(event.type)}</span>
            ${typeSpan.outerHTML}
            <span>${formatDate(event.datetime)}</span>
            <span style="margin-left: auto;">📍&nbsp;${event.location.name}</span>
        </div>
        <p>${event.summary}</p>
        <a href="https://polisen.se${event.url}" class="report-link" target="_blank">Read full report</a>
    `;

    return reportItem;
}

function normalizeString(str) {
    return str.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

function shouldShowReport(report) {
    const typeMatch = activeFilters.size === 0 || activeFilters.has(report.event.type);
    const searchMatch = searchTerm === '' ||
        normalizeString(report.event.name).includes(normalizeString(searchTerm)) ||
        normalizeString(report.event.summary).includes(normalizeString(searchTerm));
    return typeMatch && searchMatch;
}

function updateFilters() {
    let visibleCount = 0;
    markers.clearLayers();  // Clear all markers

    allReports.forEach((report, id) => {
        const shouldShow = shouldShowReport(report);
        if (report.element) {
            report.element.classList.toggle('filtered-out', !shouldShow);
        }
        if (shouldShow) {
            markers.addLayer(report.marker);  // Add marker back if it should be shown
            visibleCount++;
        }
    });

    filterInfo.textContent = `Showing ${visibleCount} of ${allReports.size} reports`;
    map.addLayer(markers);  // Re-add the marker cluster group to the map
}

function setupFilterOptions(types) {
    filterOptions.innerHTML = '';  // Clear existing options
    types.forEach(type => {
        const option = document.createElement('div');
        option.className = 'filter-option';
        option.innerHTML = `
            <label>
                <input type="checkbox" value="${type}" checked>
                <span class="crime-type" style="background-color: ${getColorForType(type)}; color: ${getContrastColor(getColorForType(type))}">${type}</span>
            </label>
        `;
        filterOptions.appendChild(option);

        option.querySelector('input').addEventListener('change', (e) => {
            if (e.target.checked) {
                activeFilters.add(type);
            } else {
                activeFilters.delete(type);
            }
            updateFilters();
        });
    });
}

resetFilters.addEventListener('click', () => {
    activeFilters.clear();
    document.querySelectorAll('#filter-options input').forEach(input => input.checked = false);
    updateFilters();
});

selectAllFilters.addEventListener('click', () => {
    const allTypes = new Set(Array.from(allReports.values()).map(report => report.event.type));
    allTypes.forEach(type => activeFilters.add(type));
    document.querySelectorAll('#filter-options input').forEach(input => input.checked = true);
    updateFilters();
});

searchBar.addEventListener('input', (e) => {
    searchTerm = e.target.value;
    updateFilters();
});

toggleSidebar.addEventListener('click', () => {
    sidebar.classList.toggle('collapsed');
});

fetch('events.json')
    .then(response => response.json())
    .then(data => {
        loading.style.display = 'none';
        const types = new Set();

        data.forEach((event, index) => {
            types.add(event.type);
            const [lat, lon] = event.location.gps.split(',');

            const icon = L.divIcon({
                className: 'custom-div-icon',
                html: `<div style="background-color:${getColorForType(event.type)};width:10px;height:10px;border-radius:50%;"></div>`,
                iconSize: [10, 10],
                iconAnchor: [5, 5]
            });

            const marker = L.marker([lat, lon], {icon: icon});
            const reportItem = createReportItem(event);
            reportsList.appendChild(reportItem);

            marker.on('click', () => {
                reportItem.scrollIntoView({ behavior: 'smooth', block: 'start' });
                reportItem.style.backgroundColor = '#ffffd0';
                setTimeout(() => {
                    reportItem.style.backgroundColor = '';
                }, 2000);
            });

            allReports.set(event.id, { event, element: reportItem, marker });
        });

        setupFilterOptions(Array.from(types).sort());
        updateFilters();

        // Save filter preferences to local storage
        window.addEventListener('beforeunload', () => {
            localStorage.setItem('activeFilters', JSON.stringify(Array.from(activeFilters)));
            localStorage.setItem('searchTerm', searchTerm);
        });

        // Load filter preferences from local storage
        const savedFilters = JSON.parse(localStorage.getItem('activeFilters'));
        if (savedFilters) {
            activeFilters = new Set(savedFilters);
            document.querySelectorAll('#filter-options input').forEach(input => {
                input.checked = activeFilters.has(input.value);
            });
        }
        const savedSearchTerm = localStorage.getItem('searchTerm');
        if (savedSearchTerm) {
            searchBar.value = savedSearchTerm;
            searchTerm = savedSearchTerm;
        }
        updateFilters();
    })
    .catch(error => {
        console.error('Error loading data:', error);
        loading.textContent = 'Error loading data';
    });

// Ensure the map fills the available space
function resizeMap() {
    const mapContainer = document.getElementById('map-container');
    const map = document.getElementById('map');
    map.style.height = `${mapContainer.offsetHeight}px`;
}

window.addEventListener('resize', resizeMap);
resizeMap();
