(function () {
    const socket = window.io ? window.io() : null;
    const OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/";
    const OSRM_TRIP_URL = "https://router.project-osrm.org/trip/v1/driving/";
    const OSRM_MATCH_URL = "https://router.project-osrm.org/match/v1/driving/";
    const STREETS_MAX_ZOOM = 19;
    const SATELLITE_NATIVE_MAX_ZOOM = 17;
    const SATELLITE_MAX_ZOOM = 20;
    const LOCATION_FOCUS_ZOOM = 18;
    const GPS_MAX_RENDER_ACCURACY = 120;
    const GPS_JITTER_DISTANCE_METERS = 8;
    const appState = {
        driverWatchId: null,
        wakeLock: null,
        routeId: null,
        routePin: null,
        driverMapContext: null,
        driverRouteActive: false,
        adminMapContext: null,
        adminTracePoints: 0,
        adminPollId: null,
        lastVoiceMessage: null,
        lastVoiceAt: 0,
    };

    function formatTimestamp(value) {
        if (!value) {
            return "Sin datos";
        }
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return value;
        }
        return date.toLocaleTimeString("es-CO", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }

    function setTextContent(elementId, value) {
        const node = document.getElementById(elementId);
        if (node) {
            node.textContent = value;
        }
    }

    function setDriverNotice(message) {
        setTextContent("driverNotice", message);
    }

    function setVoiceStatus(label) {
        setTextContent("driverVoiceStatus", label);
    }

    function setGpsStatus(status, label) {
        const node = document.getElementById("driverGpsStatus");
        if (node) {
            node.classList.remove("is-ready", "is-active", "is-warning", "is-error");
            node.classList.add(`is-${status}`);
        }
        setTextContent("driverGpsStatusText", label);
    }

    function isSecureLocationContext() {
        return window.isSecureContext || ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
    }

    function appendChatMessage(container, author, content) {
        if (!container) {
            return;
        }
        const placeholder = container.querySelector(".chat-box__empty, .text-secondary.small");
        if (placeholder) {
            placeholder.remove();
        }
        const message = document.createElement("div");
        const title = document.createElement("strong");
        const paragraph = document.createElement("p");
        message.className = "chat-message";
        message.dataset.author = (author || "S").trim().charAt(0).toUpperCase();
        title.className = "chat-message__author";
        paragraph.className = "chat-message__body mb-0";
        title.textContent = author || "Sistema";
        paragraph.textContent = content || "";
        message.appendChild(title);
        message.appendChild(paragraph);
        container.appendChild(message);
        container.scrollTop = container.scrollHeight;
    }

    function updateAttendanceFeed(payload) {
        const feed = document.getElementById("attendanceFeed");
        if (!feed) {
            return;
        }
        const selector = `[data-student-id="${payload.estudiante_id}"]`;
        let item = feed.querySelector(selector);
        if (!item) {
            item = document.createElement("div");
            item.className = "attendance-live-item tracking-student";
            item.dataset.studentId = payload.estudiante_id;
            const avatar = document.createElement("span");
            avatar.className = "tracking-student__avatar";
            avatar.textContent = (payload.estudiante || "E").trim().charAt(0).toUpperCase();
            const info = document.createElement("div");
            info.className = "tracking-student__copy";
            const name = document.createElement("strong");
            const status = document.createElement("span");
            status.className = "text-secondary small js-attendance-status";
            name.textContent = payload.estudiante || "Estudiante";
            info.appendChild(name);
            item.appendChild(avatar);
            item.appendChild(info);
            item.appendChild(status);
            feed.prepend(item);
        }
        const estado = payload.estado || "pendiente";
        item.dataset.estado = estado;
        const statusNode = item.querySelector(".js-attendance-status");
        if (statusNode) {
            statusNode.textContent = estado;
        }
    }

    function updateRouteStatus(payload) {
        const active = Boolean(payload.recorrido_activo);
        appState.driverRouteActive = active;
        const routeLabel = active ? "En recorrido" : "En espera";
        setTextContent("liveRouteState", routeLabel);
        ["driverRouteState", "driverRouteStateHero"].forEach((nodeId) => {
            setTextContent(nodeId, routeLabel);
            const node = document.getElementById(nodeId);
            if (node && node.parentElement) {
                node.parentElement.classList.toggle("is-live", active);
                node.parentElement.classList.toggle("is-idle", !active);
            }
        });
        const badge = document.getElementById("routeStatusBadge");
        if (badge) {
            badge.textContent = active ? "Ruta en vivo" : "Ruta en espera";
            badge.classList.toggle("text-bg-success", active);
            badge.classList.toggle("text-bg-secondary", !active);
        }
    }

    function formatDistance(value) {
        const meters = Number(value);
        if (!Number.isFinite(meters)) {
            return "--";
        }
        if (meters >= 1000) {
            return `${(meters / 1000).toFixed(1)} km`;
        }
        return `${Math.round(meters)} m`;
    }

    function haversineMeters(origin, target) {
        if (!origin || !target) {
            return null;
        }
        const lat1 = Number(origin[0]) * Math.PI / 180;
        const lng1 = Number(origin[1]) * Math.PI / 180;
        const lat2 = Number(target[0]) * Math.PI / 180;
        const lng2 = Number(target[1]) * Math.PI / 180;
        const dLat = lat2 - lat1;
        const dLng = lng2 - lng1;
        const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
        return 6371000 * (2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a)));
    }

    function buildInstructionText(step) {
        if (!step || !step.maneuver) {
            return null;
        }
        const type = step.maneuver.type || "continue";
        const modifier = step.maneuver.modifier || "";
        const street = step.name ? ` por ${step.name}` : "";
        const mappings = {
            depart: "Inicia la ruta",
            arrive: "Has llegado",
            turn: modifier === "right" ? "Gira a la derecha" : modifier === "left" ? "Gira a la izquierda" : "Haz un giro",
            new_name: "Continua",
            continue: "Sigue recto",
            roundabout: "Entra a la glorieta",
            rotary: "Entra a la glorieta",
            merge: "Incorporate",
            fork: modifier === "right" ? "Toma la salida derecha" : modifier === "left" ? "Toma la salida izquierda" : "Toma la bifurcacion",
            end_of_road: modifier === "right" ? "Al final, gira a la derecha" : modifier === "left" ? "Al final, gira a la izquierda" : "Al final de la via, gira",
            use_lane: "Ubicate en el carril indicado",
        };
        const base = mappings[type] || "Continua";
        const distanceSuffix = step.distance && step.distance > 25 ? ` durante ${formatDistance(step.distance)}` : "";
        return `${base}${street}${distanceSuffix}`.trim();
    }

    function pickPrimaryStep(steps) {
        if (!Array.isArray(steps) || !steps.length) {
            return null;
        }
        const preferred = steps.find(function (step) {
            return step && step.maneuver && !["depart", "arrive"].includes(step.maneuver.type);
        });
        return preferred || steps[0];
    }

    function speakNavigation(message) {
        if (!message || !window.speechSynthesis) {
            setVoiceStatus("No disponible");
            return;
        }
        const now = Date.now();
        if (appState.lastVoiceMessage === message && now - appState.lastVoiceAt < 18000) {
            return;
        }
        const utterance = new SpeechSynthesisUtterance(message);
        utterance.lang = "es-CO";
        utterance.rate = 1;
        utterance.pitch = 1;
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(utterance);
        appState.lastVoiceMessage = message;
        appState.lastVoiceAt = now;
        setVoiceStatus("Activa");
    }

    function updateLocationFields(payload, coordinatesId, timeId) {
        const lat = parseFloat(payload.latitud);
        const lng = parseFloat(payload.longitud);
        if (!Number.isNaN(lat) && !Number.isNaN(lng)) {
            setTextContent(coordinatesId, `${lat.toFixed(5)}, ${lng.toFixed(5)}`);
        }
        setTextContent(timeId, formatTimestamp(payload.ultima_ubicacion_en));
    }

    function createMap(node) {
        if (!node || !window.L) {
            return null;
        }
        const lat = parseFloat(node.dataset.lat);
        const lng = parseFloat(node.dataset.lng);
        const zoom = parseInt(node.dataset.zoom, 10);
        const isDriverMap = node.classList.contains("driver-map");
        const map = L.map(node, {
            zoomControl: true,
            attributionControl: true,
            maxZoom: STREETS_MAX_ZOOM
        }).setView([lat, lng], zoom);

        const streetsLayer = L.tileLayer(
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
            {
                maxZoom: STREETS_MAX_ZOOM,
                attribution: 'Tiles &copy; Esri, DeLorme, NAVTEQ, TomTom, Intermap, increment P Corp., GEBCO, USGS, FAO, NPS, NRCAN, GeoBase, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), and the GIS User Community'
            }
        );
        const satelliteImageryLayer = L.tileLayer(
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            {
                maxZoom: SATELLITE_MAX_ZOOM,
                maxNativeZoom: SATELLITE_NATIVE_MAX_ZOOM,
                attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community"
            }
        );
        const satelliteLabelsLayer = L.tileLayer(
            "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
            {
                maxZoom: SATELLITE_MAX_ZOOM,
                maxNativeZoom: SATELLITE_NATIVE_MAX_ZOOM,
                attribution: "Labels &copy; Esri"
            }
        );
        const satelliteLayer = L.layerGroup([
            satelliteImageryLayer,
            satelliteLabelsLayer
        ]);
        streetsLayer.addTo(map);
        map.zoomControl.setPosition("topright");
        L.control.layers(
            { "Calles": streetsLayer, "Satélite": satelliteLayer },
            null,
            { collapsed: false, position: "topright" }
        ).addTo(map);
        map.on("baselayerchange", function (event) {
            const isSatelliteLayer = event.layer === satelliteLayer;
            const targetMaxZoom = isSatelliteLayer ? SATELLITE_MAX_ZOOM : STREETS_MAX_ZOOM;
            map.setMaxZoom(targetMaxZoom);
            if (map.getZoom() > targetMaxZoom) {
                map.setZoom(targetMaxZoom);
            }
        });
        L.control.scale({ imperial: false, position: "bottomleft" }).addTo(map);

        const vehicleIcon = L.divIcon({
            className: isDriverMap ? "vehicle-marker is-driver" : "vehicle-marker",
            html: '<div class="vehicle-marker-pulse"></div><div class="vehicle-marker-icon"><i class="bi bi-bus-front-fill"></i></div>',
            iconSize: [48, 48],
            iconAnchor: [24, 24]
        });

        const marker = L.marker([lat, lng], {
            icon: vehicleIcon,
            zIndexOffset: 1000,
            riseOnHover: true,
        }).addTo(map);
        const routeOutline = L.polyline([], {
            color: "#ffffff",
            weight: 8,
            opacity: 0.95,
            lineCap: "round",
            lineJoin: "round",
        }).addTo(map);
        const routeLine = L.polyline([], {
            // Rastro del bus: azul tipo Google Maps.
            color: "#1a73e8",
            weight: 5,
            opacity: 1,
            lineCap: "round",
            lineJoin: "round",
        }).addTo(map);
        const hasLocation = node.dataset.hasLocation === "true";
        if (!hasLocation) {
            marker.setOpacity(0);
        }
        const mapContext = {
            map: map,
            isDriverMap: isDriverMap,
            marker: marker,
            routeOutline: routeOutline,
            routeLine: routeLine,
            plannedRouteOutline: L.polyline([], {
                color: "#ffffff",
                weight: 9,
                opacity: 0.9,
                lineCap: "round",
                lineJoin: "round",
            }).addTo(map),
            plannedRouteLine: L.polyline([], {
                // Ruta sugerida hacia las recogidas: azul oscuro estilo Google Maps.
                color: "#0b57d0",
                weight: 6,
                opacity: 1,
                lineCap: "round",
                lineJoin: "round",
            }).addTo(map),
            stopLayer: L.layerGroup().addTo(map),
            destinationLayer: L.layerGroup().addTo(map),
            latestLocation: null,
            hasLocation: hasLocation,
            followLocation: true,
            planData: null,
            stops: [],
            destination: null,
            lastNextStopId: null,
            arrivalNoticeByStop: {},
            lastPlanRequestAt: 0,
            rawRoutePoints: [],
            lastTraceRequestAt: 0,
            lastTraceSignature: null,
            plannedRouteFitted: false,
            optimizedStopIds: [],
            optimizedOrigin: null,
            optimizedUsingLiveLocation: false,
            lastOptimizationSignature: null,
            plannedRouteUsesLiveLocation: false,
            selectedStopId: null,
        };

        function syncCenterControls() {
            const disabled = !mapContext.hasLocation;
            if (mapContext.centerButton) {
                mapContext.centerButton.disabled = disabled;
            }
            if (mapContext.inlineCenterButton) {
                mapContext.inlineCenterButton.disabled = disabled;
                mapContext.inlineCenterButton.classList.toggle("is-disabled", disabled);
            }
        }

        function focusVehicleLocation() {
            if (!mapContext.hasLocation) {
                return;
            }
            mapContext.followLocation = true;
            const minimumZoom = map.hasLayer(satelliteLayer) ? LOCATION_FOCUS_ZOOM : 17;
            const targetZoom = Math.min(map.getMaxZoom(), Math.max(map.getZoom(), minimumZoom));
            map.setView(mapContext.marker.getLatLng(), targetZoom, { animate: true });
        }

        map.on("dragstart", function () {
            mapContext.followLocation = false;
        });
        map.on("zoomend", function () {
            if (!mapContext.followLocation || !mapContext.hasLocation) {
                return;
            }
            map.panInside(mapContext.marker.getLatLng(), {
                paddingTopLeft: [40, 40],
                paddingBottomRight: [40, 40],
                animate: true,
            });
        });

        mapContext.centerButton = document.querySelector(
            `.js-map-center[data-map-target="${node.id}"]`
        );
        if (mapContext.centerButton) {
            mapContext.centerButton.addEventListener("click", focusVehicleLocation);
        }

        const InlineCenterControl = L.Control.extend({
            options: { position: "topleft" },
            onAdd: function () {
                const container = L.DomUtil.create("div", "leaflet-bar map-inline-center-control");
                const button = L.DomUtil.create("button", "map-inline-center-button", container);
                button.type = "button";
                button.title = "Centrar ubicacion";
                button.setAttribute("aria-label", "Centrar ubicacion");
                button.innerHTML = '<i class="bi bi-crosshair"></i>';
                L.DomEvent.disableClickPropagation(container);
                L.DomEvent.disableScrollPropagation(container);
                L.DomEvent.on(button, "click", function (event) {
                    L.DomEvent.stop(event);
                    focusVehicleLocation();
                });
                mapContext.inlineCenterButton = button;
                return container;
            }
        });
        map.addControl(new InlineCenterControl());
        syncCenterControls();
        // Leaflet mide el contenedor al inicializar. Si el diseno termina de
        // acomodarse despues (movil o PWA instalada) hay que recalcular el
        // tamano para que la ruta y las capas se dibujen correctamente.
        window.setTimeout(function () {
            map.invalidateSize();
        }, 250);
        window.addEventListener("resize", function () {
            map.invalidateSize();
        });
        return mapContext;
    }

    function createHouseIcon(index, completed, selected) {
        const label = Number.isFinite(index) ? `<span class="house-marker-order">${index}</span>` : "";
        return L.divIcon({
            className: `house-marker${completed ? " is-complete" : ""}${selected ? " is-selected" : ""}`,
            html: `<div class="house-marker-shell">${label}<i class="bi bi-house-door-fill"></i></div>`,
            iconSize: [34, 34],
            iconAnchor: [17, 17]
        });
    }

    function createDestinationIcon() {
        return L.divIcon({
            className: "destination-marker",
            html: '<div class="destination-marker-shell"><i class="bi bi-flag-fill"></i></div>',
            iconSize: [34, 34],
            iconAnchor: [17, 17]
        });
    }

    function raiseRouteLayers(mapContext) {
        // Orden fijo de capas: rastro real debajo y ruta sugerida siempre visible encima.
        if (!mapContext || !mapContext.map) {
            return;
        }
        // Si alguna capa se desprendio del mapa se vuelve a agregar para que
        // la ruta nunca quede invisible en el portal de familias.
        if (mapContext.routeOutline && !mapContext.map.hasLayer(mapContext.routeOutline)) {
            mapContext.routeOutline.addTo(mapContext.map);
        }
        if (mapContext.routeLine && !mapContext.map.hasLayer(mapContext.routeLine)) {
            mapContext.routeLine.addTo(mapContext.map);
        }
        if (mapContext.plannedRouteOutline && !mapContext.map.hasLayer(mapContext.plannedRouteOutline)) {
            mapContext.plannedRouteOutline.addTo(mapContext.map);
        }
        if (mapContext.plannedRouteLine && !mapContext.map.hasLayer(mapContext.plannedRouteLine)) {
            mapContext.plannedRouteLine.addTo(mapContext.map);
        }
        if (mapContext.routeOutline) {
            mapContext.routeOutline.bringToFront();
        }
        if (mapContext.routeLine) {
            mapContext.routeLine.bringToFront();
        }
        if (mapContext.plannedRouteOutline) {
            mapContext.plannedRouteOutline.bringToFront();
        }
        if (mapContext.plannedRouteLine) {
            mapContext.plannedRouteLine.bringToFront();
        }
    }

    function setPlannedRoute(mapContext, coordinates, options) {
        if (!mapContext) {
            return;
        }
        mapContext.plannedRouteOutline.setLatLngs(coordinates);
        mapContext.plannedRouteLine.setLatLngs(coordinates);
        raiseRouteLayers(mapContext);
        if (
            options &&
            options.fitBounds &&
            Array.isArray(coordinates) &&
            coordinates.length > 1
        ) {
            mapContext.map.fitBounds(mapContext.plannedRouteLine.getBounds(), { padding: [36, 36] });
        }
    }

    function getOrderedStops(mapContext) {
        if (!mapContext || !Array.isArray(mapContext.stops)) {
            return [];
        }
        const optimizedIds = Array.isArray(mapContext.optimizedStopIds) ? mapContext.optimizedStopIds : [];
        return mapContext.stops.slice().sort(function (left, right) {
            const leftOptimizedIndex = optimizedIds.indexOf(left.id);
            const rightOptimizedIndex = optimizedIds.indexOf(right.id);
            if (leftOptimizedIndex !== -1 || rightOptimizedIndex !== -1) {
                if (leftOptimizedIndex === -1) {
                    return 1;
                }
                if (rightOptimizedIndex === -1) {
                    return -1;
                }
                return leftOptimizedIndex - rightOptimizedIndex;
            }
            const leftOrder = Number.isFinite(Number(left.orden)) ? Number(left.orden) : Number.MAX_SAFE_INTEGER;
            const rightOrder = Number.isFinite(Number(right.orden)) ? Number(right.orden) : Number.MAX_SAFE_INTEGER;
            if (leftOrder !== rightOrder) {
                return leftOrder - rightOrder;
            }
            return String(left.nombre || "").localeCompare(String(right.nombre || ""), "es");
        });
    }

    function resetStopProgress(mapContext) {
        if (!mapContext || !Array.isArray(mapContext.stops)) {
            return;
        }
        mapContext.lastNextStopId = null;
        mapContext.arrivalNoticeByStop = {};
        getOrderedStops(mapContext).forEach(function (stop, index) {
            stop.completada = false;
            updateStopMarkerState(stop, index);
        });
    }

    function applyOptimizedStopOrder(mapContext, orderedStops, origin, usedLiveLocation) {
        if (!mapContext) {
            return;
        }
        const stops = Array.isArray(orderedStops) && orderedStops.length ? orderedStops.slice() : getOrderedStops(mapContext);
        mapContext.optimizedStopIds = stops.map(function (stop) {
            return stop.id;
        });
        stops.forEach(function (stop, index) {
            updateStopMarkerState(stop, index);
        });
        mapContext.optimizedOrigin = Array.isArray(origin) ? origin.slice() : null;
        mapContext.optimizedUsingLiveLocation = Boolean(usedLiveLocation);
        renderDriverHud(mapContext);
    }

    function getSelectedStop(mapContext) {
        if (!mapContext || !Array.isArray(mapContext.stops) || !mapContext.selectedStopId) {
            return null;
        }
        const selectedStop = mapContext.stops.find(function (stop) {
            return stop.id === mapContext.selectedStopId;
        }) || null;
        if (!selectedStop) {
            mapContext.selectedStopId = null;
        }
        return selectedStop;
    }

    function getRouteTargetStops(mapContext) {
        const selectedStop = getSelectedStop(mapContext);
        if (selectedStop) {
            return [selectedStop];
        }
        return getRemainingStops(mapContext);
    }

    function renderRoutePlan(mapContext, payload) {
        if (!mapContext || !payload) {
            return;
        }
        const previousSelectedStopId = mapContext.selectedStopId;
        mapContext.planData = payload;
        mapContext.lastNextStopId = null;
        mapContext.lastPlanRequestAt = 0;
        mapContext.lastOptimizationSignature = null;
        mapContext.optimizedOrigin = null;
        mapContext.optimizedUsingLiveLocation = false;
        mapContext.optimizedStopIds = [];
        mapContext.plannedRouteFitted = false;
        mapContext.stopLayer.clearLayers();
        mapContext.destinationLayer.clearLayers();
        mapContext.arrivalNoticeByStop = {};

        mapContext.stops = (payload.paradas || [])
            .filter(function (stop) {
                return stop && stop.activo !== false && Number.isFinite(Number(stop.latitud)) && Number.isFinite(Number(stop.longitud));
            })
            .map(function (stop, index) {
                const marker = L.marker(
                    [Number(stop.latitud), Number(stop.longitud)],
                    {
                        icon: createHouseIcon(index + 1, false, previousSelectedStopId === stop.id),
                        draggable: Boolean(mapContext.allowHouseEditing),
                    }
                ).addTo(mapContext.stopLayer);
                marker.bindPopup(
                    `<strong>${stop.nombre || "Parada"}</strong><br>${stop.direccion || "Direccion pendiente"}`
                );
                marker.on("click", function () {
                    const sameStopSelected = mapContext.selectedStopId === stop.id;
                    mapContext.selectedStopId = sameStopSelected ? null : stop.id;
                    mapContext.plannedRouteFitted = false;
                    getOrderedStops(mapContext).forEach(function (currentStop, currentIndex) {
                        updateStopMarkerState(currentStop, currentIndex);
                    });
                    updateNavigationSummary(mapContext);
                    refreshPlannedRoute(mapContext);
                    if (mapContext.isDriverMap) {
                        setDriverNotice(
                            sameStopSelected
                                ? "Volviste al trazado automatico de la ruta."
                                : `Ruta fijada hacia ${stop.nombre || "la parada seleccionada"}.`
                        );
                    }
                });
                if (mapContext.allowHouseEditing) {
                    marker.on("dragend", function () {
                        const position = marker.getLatLng();
                        saveHouseLocation(stop.id, position.lat, position.lng);
                    });
                }
                return {
                    id: stop.id,
                    nombre: stop.nombre,
                    direccion: stop.direccion,
                    referencia: stop.referencia,
                    coordenadas: [Number(stop.latitud), Number(stop.longitud)],
                    marker: marker,
                    mapContext: mapContext,
                    orden: stop.orden_parada,
                    completada: false,
                };
            });
        mapContext.selectedStopId = mapContext.stops.some(function (stop) {
            return stop.id === previousSelectedStopId;
        }) ? previousSelectedStopId : null;

        if (
            payload.destino_coords &&
            Number.isFinite(Number(payload.destino_coords.latitud)) &&
            Number.isFinite(Number(payload.destino_coords.longitud))
        ) {
            mapContext.destination = {
                nombre: payload.destino || "Destino",
                coordenadas: [Number(payload.destino_coords.latitud), Number(payload.destino_coords.longitud)],
            };
            const destinationMarker = L.marker(mapContext.destination.coordenadas, { icon: createDestinationIcon() }).addTo(mapContext.destinationLayer);
            destinationMarker.bindPopup(`<strong>${mapContext.destination.nombre}</strong>`);
        } else {
            mapContext.destination = null;
        }
        applyOptimizedStopOrder(mapContext, mapContext.stops, getCurrentOrigin(mapContext), false);
    }

    function setRouteHistory(mapContext, points) {
        if (!mapContext || !Array.isArray(points)) {
            return;
        }
        mapContext.rawRoutePoints = [];
        points.forEach(function (point) {
            appendRawRoutePoint(mapContext, normalizeTracePoint(point));
        });
        refreshRecordedTrace(mapContext, true);
    }

    function normalizeTracePoint(point) {
        if (!point) {
            return null;
        }
        const lat = parseFloat(point.latitud);
        const lng = parseFloat(point.longitud);
        if (Number.isNaN(lat) || Number.isNaN(lng)) {
            return null;
        }
        const accuracyValue = point.precision ?? point.accuracy;
        const accuracy = accuracyValue === null || accuracyValue === undefined ? null : parseFloat(accuracyValue);
        const timestamp = point.registrada_en || point.ultima_ubicacion_en || null;
        return {
            lat: lat,
            lng: lng,
            accuracy: Number.isNaN(accuracy) ? null : accuracy,
            timestamp: timestamp,
        };
    }

    function appendRawRoutePoint(mapContext, point) {
        if (!mapContext || !point) {
            return;
        }
        const previous = mapContext.rawRoutePoints.length
            ? mapContext.rawRoutePoints[mapContext.rawRoutePoints.length - 1]
            : null;
        if (previous && previous.lat === point.lat && previous.lng === point.lng) {
            return;
        }
        mapContext.rawRoutePoints.push(point);
    }

    function buildRawRouteCoordinates(points) {
        return (points || []).map(function (point) {
            return [point.lat, point.lng];
        });
    }

    function drawRecordedTrace(mapContext, coordinates, shouldFitBounds) {
        if (!mapContext) {
            return;
        }
        mapContext.routeOutline.setLatLngs(coordinates);
        mapContext.routeLine.setLatLngs(coordinates);
        raiseRouteLayers(mapContext);
        if (coordinates.length > 1 && shouldFitBounds) {
            mapContext.map.fitBounds(mapContext.routeLine.getBounds(), { padding: [24, 24] });
        }
    }

    function simplifyTracePoints(points, limit) {
        if (!Array.isArray(points) || points.length <= limit) {
            return Array.isArray(points) ? points.slice() : [];
        }
        const step = (points.length - 1) / (limit - 1);
        const simplified = [];
        let lastIndex = -1;
        for (let index = 0; index < limit; index += 1) {
            const sourceIndex = Math.round(index * step);
            if (sourceIndex === lastIndex) {
                continue;
            }
            simplified.push(points[sourceIndex]);
            lastIndex = sourceIndex;
        }
        const lastPoint = points[points.length - 1];
        if (simplified[simplified.length - 1] !== lastPoint) {
            simplified.push(lastPoint);
        }
        return simplified;
    }

    function filterTracePoints(points) {
        if (!Array.isArray(points)) {
            return [];
        }
        const filtered = [];
        points.forEach(function (point) {
            if (!point) {
                return;
            }
            if (point.accuracy !== null && point.accuracy > 60) {
                return;
            }
            const previous = filtered.length ? filtered[filtered.length - 1] : null;
            if (previous && haversineMeters([previous.lat, previous.lng], [point.lat, point.lng]) < 5) {
                return;
            }
            filtered.push(point);
        });
        return filtered;
    }

    function buildTraceSignature(points) {
        if (!Array.isArray(points) || !points.length) {
            return "empty";
        }
        const first = points[0];
        const last = points[points.length - 1];
        return [
            points.length,
            first.timestamp || "",
            last.timestamp || "",
            last.lat.toFixed(5),
            last.lng.toFixed(5),
        ].join("|");
    }

    function buildMatchUrl(points) {
        const coordinates = points
            .map(function (point) {
                return `${point.lng},${point.lat}`;
            })
            .join(";");
        const radiuses = points
            .map(function (point) {
                if (point.accuracy === null || point.accuracy === undefined) {
                    return "25";
                }
                return String(Math.max(5, Math.min(50, Math.round(point.accuracy))));
            })
            .join(";");
        const hasTimestamps = points.every(function (point) {
            return Boolean(point.timestamp) && !Number.isNaN(new Date(point.timestamp).getTime());
        });
        let url = `${OSRM_MATCH_URL}${coordinates}?overview=full&geometries=geojson&gaps=ignore&tidy=true&radiuses=${radiuses}`;
        if (hasTimestamps) {
            const timestamps = points
                .map(function (point) {
                    return Math.floor(new Date(point.timestamp).getTime() / 1000);
                })
                .join(";");
            url += `&timestamps=${timestamps}`;
        }
        return url;
    }

    function extractMatchedCoordinates(payload) {
        if (!payload || !Array.isArray(payload.matchings)) {
            return [];
        }
        const coordinates = [];
        payload.matchings.forEach(function (matching) {
            const geometry = matching && matching.geometry && Array.isArray(matching.geometry.coordinates)
                ? matching.geometry.coordinates
                : [];
            geometry.forEach(function (coordinate) {
                if (!Array.isArray(coordinate) || coordinate.length < 2) {
                    return;
                }
                const lat = coordinate[1];
                const lng = coordinate[0];
                const previous = coordinates.length ? coordinates[coordinates.length - 1] : null;
                if (previous && previous[0] === lat && previous[1] === lng) {
                    return;
                }
                coordinates.push([lat, lng]);
            });
        });
        return coordinates;
    }

    function refreshRecordedTrace(mapContext, shouldFitBounds) {
        if (!mapContext) {
            return;
        }
        const filteredPoints = filterTracePoints(mapContext.rawRoutePoints);
        const rawCoordinates = buildRawRouteCoordinates(filteredPoints);
        if (rawCoordinates.length < 2) {
            drawRecordedTrace(mapContext, rawCoordinates, shouldFitBounds);
            return;
        }

        const simplifiedPoints = simplifyTracePoints(filteredPoints, 90);
        const signature = buildTraceSignature(simplifiedPoints);
        if (signature === mapContext.lastTraceSignature) {
            if (!mapContext.routeLine.getLatLngs().length) {
                drawRecordedTrace(mapContext, rawCoordinates, shouldFitBounds);
            }
            return;
        }

        const now = Date.now();
        if (now - mapContext.lastTraceRequestAt < 4000) {
            drawRecordedTrace(mapContext, rawCoordinates, shouldFitBounds);
            return;
        }

        mapContext.lastTraceRequestAt = now;
        mapContext.lastTraceSignature = signature;
        fetch(buildMatchUrl(simplifiedPoints))
            .then(function (response) {
                if (!response.ok) {
                    throw new Error(`No se pudo ajustar el recorrido a las vias (${response.status}).`);
                }
                return response.json();
            })
            .then(function (payload) {
                const matchedCoordinates = extractMatchedCoordinates(payload);
                if (matchedCoordinates.length > 1) {
                    drawRecordedTrace(mapContext, matchedCoordinates, shouldFitBounds);
                    return;
                }
                drawRecordedTrace(mapContext, rawCoordinates, shouldFitBounds);
            })
            .catch(function (error) {
                window.console.error("No fue posible alinear el recorrido con las vias.", error);
                drawRecordedTrace(mapContext, rawCoordinates, shouldFitBounds);
            });
    }

    function loadRouteHistory(mapContext, rutaId, pin, onError) {
        const endpoint = `/tracking/api/ruta/${rutaId}/recorrido?pin=${encodeURIComponent(pin)}`;
        fetch(endpoint)
            .then(function (response) {
                if (!response.ok) {
                    throw new Error(`No se pudo cargar el recorrido (${response.status}).`);
                }
                return response.json();
            })
            .then(function (payload) {
                setRouteHistory(mapContext, payload.puntos);
                const latestPoint = Array.isArray(payload.puntos) && payload.puntos.length
                    ? payload.puntos[payload.puntos.length - 1]
                    : null;
                if (latestPoint) {
                    updateMapLocation(mapContext, {
                        latitud: latestPoint.latitud,
                        longitud: latestPoint.longitud,
                        precision: latestPoint.precision,
                        ultima_ubicacion_en: latestPoint.registrada_en || latestPoint.ultima_ubicacion_en || null,
                    });
                }
            })
            .catch(function (error) {
                window.console.error("No fue posible cargar el recorrido de la ruta.", error);
                if (onError) {
                    onError(error);
                }
            });
    }

    function loadRoutePlan(mapContext, rutaId, pin, onError) {
        const endpoint = `/tracking/api/ruta/${rutaId}/plan?pin=${encodeURIComponent(pin)}`;
        fetch(endpoint)
            .then(function (response) {
                if (!response.ok) {
                    throw new Error(`No se pudo cargar el plan de la ruta (${response.status}).`);
                }
                return response.json();
            })
            .then(function (payload) {
                renderRoutePlan(mapContext, payload);
                refreshPlannedRoute(mapContext);
                // Red de seguridad: si la ruta no alcanzo a dibujarse (por red,
                // OSRM lento o pestana en segundo plano) se reintenta enseguida.
                window.setTimeout(function () {
                    if (
                        mapContext.plannedRouteLine &&
                        !mapContext.plannedRouteLine.getLatLngs().length
                    ) {
                        refreshPlannedRoute(mapContext);
                    }
                }, 1500);
            })
            .catch(function (error) {
                window.console.error("No fue posible cargar las paradas de la ruta.", error);
                if (onError) {
                    onError(error);
                }
            });
    }

    function saveHouseLocation(estudianteId, lat, lng) {
        const mapContext = appState.driverMapContext;
        if (!mapContext || !estudianteId) {
            return;
        }
        const body = new URLSearchParams();
        body.append("lat", lat);
        body.append("lng", lng);

        setDriverNotice("Guardando la ubicacion de la casita...");
        fetch(`/conductor/estudiantes/${estudianteId}/ubicacion`, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8" },
            body: body.toString(),
        })
            .then(function (response) {
                return response.text().then(function (text) {
                    let data = null;
                    try {
                        data = JSON.parse(text);
                    } catch (error) {
                        data = null;
                    }
                    return { status: response.status, data: data };
                });
            })
            .then(function (result) {
                if (result.status !== 200 || !result.data || !result.data.ok) {
                    throw new Error((result.data && result.data.error) || "No fue posible guardar la casita.");
                }
                markHouseAsPlaced(result.data.estudiante_id);
                setDriverNotice(`${result.data.nombre}: casita ubicada. La ruta se esta recalculando.`);
                loadRoutePlan(mapContext, mapContext.rutaId, mapContext.pin, function () {
                    setDriverNotice("No fue posible refrescar las casitas y la ruta sugerida.");
                });
            })
            .catch(function (error) {
                setDriverNotice(error.message || "No fue posible guardar la ubicacion de la casita.");
                window.console.error("Error al ubicar la casita.", error);
            });
    }

    function markHouseAsPlaced(estudianteId) {
        const select = document.getElementById("housePlacementSelect");
        const hint = document.getElementById("housePlacementHint");
        if (select) {
            const option = select.querySelector(`option[value="${estudianteId}"]`);
            if (option) {
                option.remove();
            }
            if (select.options.length > 1) {
                select.selectedIndex = 1;
            } else {
                select.selectedIndex = 0;
            }
        }
        if (hint) {
            hint.textContent = select && select.options.length > 1
                ? "Casita guardada. Ya puedes ubicar el siguiente estudiante pendiente."
                : "Casita guardada. Ya no quedan estudiantes pendientes por ubicar.";
        }
    }

    function getCurrentOrigin(mapContext) {
        if (mapContext && Array.isArray(mapContext.latestLocation)) {
            return mapContext.latestLocation;
        }
        if (
            mapContext &&
            mapContext.planData &&
            mapContext.planData.origen_coords &&
            Number.isFinite(Number(mapContext.planData.origen_coords.latitud)) &&
            Number.isFinite(Number(mapContext.planData.origen_coords.longitud))
        ) {
            return [
                Number(mapContext.planData.origen_coords.latitud),
                Number(mapContext.planData.origen_coords.longitud),
            ];
        }
        if (mapContext && mapContext.hasLocation) {
            const location = mapContext.marker.getLatLng();
            return [location.lat, location.lng];
        }
        return null;
    }

    function getRemainingStops(mapContext) {
        return getOrderedStops(mapContext).filter(function (stop) {
            return !stop.completada;
        });
    }

    function updateStopMarkerState(stop, index) {
        if (!stop || !stop.marker) {
            return;
        }
        const isSelected = Boolean(stop.mapContext && stop.mapContext.selectedStopId === stop.id);
        stop.marker.setIcon(createHouseIcon(index + 1, stop.completada, isSelected));
    }

    function refreshStopProgress(mapContext) {
        if (!mapContext || !Array.isArray(mapContext.latestLocation)) {
            return;
        }
        getOrderedStops(mapContext).forEach(function (stop, index) {
            const distance = haversineMeters(mapContext.latestLocation, stop.coordenadas);
            if (!stop.completada && distance !== null && distance <= 80) {
                stop.completada = true;
                updateStopMarkerState(stop, index);
                if (mapContext.isDriverMap) {
                    speakNavigation(`Llegando a la parada de ${stop.nombre}.`);
                }
            }
        });
    }

    function updateNavigationSummary(mapContext) {
        if (!mapContext || !mapContext.isDriverMap) {
            return;
        }
        const remainingStops = getRemainingStops(mapContext);
        const selectedStop = getSelectedStop(mapContext);
        if (!remainingStops.length && !selectedStop) {
            setTextContent("driverNextStop", mapContext.destination ? mapContext.destination.nombre : "Ruta completada");
            setTextContent("driverNextDistance", mapContext.destination && mapContext.latestLocation ? formatDistance(haversineMeters(mapContext.latestLocation, mapContext.destination.coordenadas)) : "--");
            renderDriverHud(mapContext);
            return;
        }
        const nextStop = selectedStop || remainingStops[0];
        const distance = mapContext.latestLocation ? haversineMeters(mapContext.latestLocation, nextStop.coordenadas) : null;
        const stopLabel = nextStop.direccion ? `${nextStop.nombre} · ${nextStop.direccion}` : (nextStop.nombre || "Siguiente parada");
        setTextContent("driverNextStop", stopLabel);
        setTextContent("driverNextDistance", formatDistance(distance));
        if (!appState.driverRouteActive && nextStop.direccion) {
            setTextContent("driverVoiceHint", `Siguiente recogida: ${nextStop.direccion}`);
        }
        if (mapContext.lastNextStopId !== nextStop.id && appState.driverRouteActive) {
            speakNavigation(`Proxima parada, ${nextStop.nombre}.`);
            mapContext.lastNextStopId = nextStop.id;
        }
        if (distance !== null && distance <= 150 && !mapContext.arrivalNoticeByStop[nextStop.id] && appState.driverRouteActive) {
            speakNavigation(`Estas cerca de la parada de ${nextStop.nombre}.`);
            mapContext.arrivalNoticeByStop[nextStop.id] = true;
        }
        renderDriverHud(mapContext);
    }

    function getNavigationWaypoints(mapContext) {
        if (!mapContext) {
            return [];
        }
        const coordinates = [];
        const origin = getCurrentOrigin(mapContext);
        if (origin) {
            coordinates.push(origin);
        }
        getRouteTargetStops(mapContext).forEach(function (stop) {
            coordinates.push(stop.coordenadas);
        });
        if (!getSelectedStop(mapContext) && mapContext.destination) {
            coordinates.push(mapContext.destination.coordenadas);
        }
        return coordinates.filter(Boolean);
    }

    function buildOptimizationSignature(mapContext, origin) {
        const originSignature = Array.isArray(origin)
            ? `${origin[0].toFixed(3)},${origin[1].toFixed(3)}`
            : "no-origin";
        const stopsSignature = getOrderedStops(mapContext).map(function (stop) {
            return `${stop.id}:${stop.coordenadas[0].toFixed(5)},${stop.coordenadas[1].toFixed(5)}`;
        }).join("|");
        const destinationSignature = mapContext && mapContext.destination
            ? `${mapContext.destination.coordenadas[0].toFixed(5)},${mapContext.destination.coordenadas[1].toFixed(5)}`
            : "no-destination";
        return `${originSignature}::${stopsSignature}::${destinationSignature}`;
    }

    function buildTripUrl(origin, stops, destination) {
        const coordinates = [origin]
            .concat(stops.map(function (stop) {
                return stop.coordenadas;
            }))
            .concat(destination ? [destination.coordenadas] : [])
            .map(function (pair) {
                return `${pair[1]},${pair[0]}`;
            })
            .join(";");
        const params = [
            "overview=full",
            "geometries=geojson",
            "steps=true",
            "source=first",
            "roundtrip=false",
        ];
        if (destination) {
            params.push("destination=last");
        }
        return `${OSRM_TRIP_URL}${coordinates}?${params.join("&")}`;
    }

    function extractTripStopOrder(payload, stops, hasDestination) {
        if (!payload || !Array.isArray(payload.waypoints)) {
            return stops.slice();
        }
        return payload.waypoints
            .slice(1, hasDestination ? -1 : undefined)
            .map(function (waypoint, inputIndex) {
                return {
                    stop: stops[inputIndex],
                    waypointIndex: waypoint && Number.isFinite(Number(waypoint.waypoint_index))
                        ? Number(waypoint.waypoint_index)
                        : inputIndex + 1,
                };
            })
            .sort(function (left, right) {
                return left.waypointIndex - right.waypointIndex;
            })
            .map(function (entry) {
                return entry.stop;
            })
            .filter(Boolean);
    }

    function optimizeStopSequence(mapContext) {
        if (!mapContext || !Array.isArray(mapContext.stops) || mapContext.stops.length < 2) {
            return Promise.resolve();
        }
        const shouldUseLiveLocation = appState.driverRouteActive && Array.isArray(mapContext.latestLocation);
        const origin = shouldUseLiveLocation && !mapContext.optimizedUsingLiveLocation
            ? mapContext.latestLocation
            : (mapContext.optimizedOrigin || getCurrentOrigin(mapContext));
        if (!Array.isArray(origin)) {
            return Promise.resolve();
        }
        const signature = buildOptimizationSignature(mapContext, origin);
        if (signature === mapContext.lastOptimizationSignature) {
            return Promise.resolve();
        }
        mapContext.lastOptimizationSignature = signature;
        return fetch(buildTripUrl(origin, mapContext.stops, mapContext.destination))
            .then(function (response) {
                if (!response.ok) {
                    throw new Error(`No se pudo optimizar la ruta (${response.status}).`);
                }
                return response.json();
            })
            .then(function (payload) {
                const orderedStops = extractTripStopOrder(payload, mapContext.stops, Boolean(mapContext.destination));
                applyOptimizedStopOrder(mapContext, orderedStops, origin, shouldUseLiveLocation);
                updateNavigationSummary(mapContext);
            })
            .catch(function (error) {
                window.console.error("No fue posible optimizar el orden de las paradas.", error);
                applyOptimizedStopOrder(mapContext, getOrderedStops(mapContext), origin, shouldUseLiveLocation);
                updateNavigationSummary(mapContext);
            });
    }

    function refreshPlannedRoute(mapContext) {
        if (!mapContext || !mapContext.planData) {
            return;
        }
        refreshStopProgress(mapContext);
        updateNavigationSummary(mapContext);
        optimizeStopSequence(mapContext).finally(function () {
            const waypoints = getNavigationWaypoints(mapContext);
            const fallbackCoordinates = waypoints.filter(Boolean);
            if (waypoints.length < 2) {
                setPlannedRoute(mapContext, []);
                return;
            }

            // Si aun no hay trazado en el mapa, se dibuja de inmediato la ruta
            // directa a las recogidas para que nunca quede sin marcar.
            if (!mapContext.plannedRouteLine.getLatLngs().length) {
                setPlannedRoute(
                    mapContext,
                    fallbackCoordinates,
                    { fitBounds: !mapContext.plannedRouteFitted }
                );
                if (!mapContext.plannedRouteFitted && fallbackCoordinates.length > 1) {
                    mapContext.plannedRouteFitted = true;
                }
            }

            const now = Date.now();
            if (now - mapContext.lastPlanRequestAt < 4000) {
                return;
            }
            mapContext.lastPlanRequestAt = now;

            const coordinates = waypoints
                .map(function (pair) {
                    return `${pair[1]},${pair[0]}`;
                })
                .join(";");
            const url = `${OSRM_ROUTE_URL}${coordinates}?overview=full&geometries=geojson&steps=true&continue_straight=true`;

            fetch(url)
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error(`No se pudo calcular la navegacion (${response.status}).`);
                    }
                    return response.json();
                })
                .then(function (payload) {
                    if (!payload.routes || !payload.routes.length) {
                        return;
                    }
                    const route = payload.routes[0];
                    const geometry = (route.geometry && route.geometry.coordinates) || [];
                    setPlannedRoute(
                        mapContext,
                        geometry.map(function (coordinate) {
                            return [coordinate[1], coordinate[0]];
                        }),
                        { fitBounds: !mapContext.plannedRouteFitted }
                    );
                    if (!mapContext.plannedRouteFitted && geometry.length > 1) {
                        mapContext.plannedRouteFitted = true;
                    }
                    if (mapContext.isDriverMap) {
                        const firstLeg = route.legs && route.legs.length ? route.legs[0] : null;
                        const firstStep = firstLeg ? pickPrimaryStep(firstLeg.steps) : null;
                        const instruction = buildInstructionText(firstStep);
                        if (instruction) {
                            setTextContent("driverVoiceHint", instruction);
                        }
                    }
                })
                .catch(function (error) {
                    window.console.error("No fue posible calcular la ruta sugerida.", error);
                    // Respaldo: mantiene visible la ruta directa hacia las recogidas.
                    setPlannedRoute(
                        mapContext,
                        fallbackCoordinates,
                        { fitBounds: !mapContext.plannedRouteFitted }
                    );
                    if (!mapContext.plannedRouteFitted && fallbackCoordinates.length > 1) {
                        mapContext.plannedRouteFitted = true;
                    }
                });
        });
    }

    function shouldRenderGpsPoint(mapContext, payload) {
        if (!mapContext || !payload) {
            return false;
        }
        const accuracy = payload.precision === null || payload.precision === undefined
            ? null
            : Number(payload.precision);
        if (
            accuracy !== null &&
            Number.isFinite(accuracy) &&
            accuracy > GPS_MAX_RENDER_ACCURACY &&
            mapContext.hasLocation
        ) {
            return false;
        }
        const lat = parseFloat(payload.latitud);
        const lng = parseFloat(payload.longitud);
        if (Number.isNaN(lat) || Number.isNaN(lng)) {
            return false;
        }
        if (Array.isArray(mapContext.latestLocation)) {
            const jump = haversineMeters(mapContext.latestLocation, [lat, lng]);
            if (jump !== null && jump < GPS_JITTER_DISTANCE_METERS) {
                return false;
            }
        }
        return true;
    }

    function updateMapLocation(mapContext, payload) {
        if (!mapContext) {
            return;
        }
        if (!shouldRenderGpsPoint(mapContext, payload)) {
            return;
        }
        const lat = parseFloat(payload.latitud);
        const lng = parseFloat(payload.longitud);
        if (Number.isNaN(lat) || Number.isNaN(lng)) {
            return;
        }
        const previousLocation = Array.isArray(mapContext.latestLocation) ? mapContext.latestLocation.slice() : null;
        mapContext.latestLocation = [lat, lng];
        if (appState.driverRouteActive && !mapContext.plannedRouteUsesLiveLocation) {
            // Al entrar la primera posicion real del bus, la ruta planeada se
            // vuelve a encuadrar desde ese punto tanto para conductor como
            // para familias.
            mapContext.plannedRouteFitted = false;
            mapContext.plannedRouteUsesLiveLocation = true;
        } else if (appState.driverRouteActive && previousLocation) {
            const movement = haversineMeters(previousLocation, mapContext.latestLocation);
            if (movement !== null && movement >= 120) {
                mapContext.plannedRouteFitted = false;
            }
        }
        mapContext.hasLocation = true;
        mapContext.marker.setOpacity(1);
        if (mapContext.centerButton || mapContext.inlineCenterButton) {
            if (mapContext.centerButton) {
                mapContext.centerButton.disabled = false;
            }
            if (mapContext.inlineCenterButton) {
                mapContext.inlineCenterButton.disabled = false;
                mapContext.inlineCenterButton.classList.remove("is-disabled");
            }
        }
        mapContext.marker.setLatLng([lat, lng]);
        appendRawRoutePoint(mapContext, normalizeTracePoint(payload));
        refreshRecordedTrace(mapContext, false);
        if (mapContext.followLocation) {
            mapContext.map.panTo([lat, lng]);
        }
        refreshPlannedRoute(mapContext);
    }

    function joinRouteRoom(rutaId, pin) {
        if (!socket || !rutaId || !pin) {
            return;
        }
        appState.routeId = rutaId;
        appState.routePin = pin;
        socket.emit("room:join", { ruta_id: rutaId, pin: pin });
    }

    function setupTrackingPage() {
        const mapNode = document.getElementById("trackingMap");
        if (!mapNode) {
            return;
        }
        const rutaId = parseInt(mapNode.dataset.rutaId, 10);
        const pin = mapNode.dataset.pin;
        const mapContext = createMap(mapNode);
        loadRouteHistory(mapContext, rutaId, pin);
        loadRoutePlan(mapContext, rutaId, pin);
        if (!socket) {
            return;
        }
        joinRouteRoom(rutaId, pin);
        socket.on("room:joined", function (payload) {
            const tracking = payload && payload.tracking;
            if (!tracking || tracking.ruta_id !== rutaId) {
                return;
            }
            // Entrega a las familias el estado y la ubicacion actuales al entrar a la sala.
            updateRouteStatus(tracking);
            updateLocationFields(tracking, "liveCoordinates", "lastUpdateAt");
            updateMapLocation(mapContext, tracking);
        });
        socket.on("tracking:location", function (payload) {
            if (payload.ruta_id !== rutaId) {
                return;
            }
            updateMapLocation(mapContext, payload);
            updateLocationFields(payload, "liveCoordinates", "lastUpdateAt");
        });
        socket.on("tracking:route_status", function (payload) {
            if (payload.ruta_id === rutaId) {
                updateRouteStatus(payload);
                if (payload.location_reset) {
                    resetMapLocation(mapContext);
                    updateLocationFields({ latitud: null, longitud: null, ultima_ubicacion_en: null }, "liveCoordinates", "lastUpdateAt");
                }
            }
        });
        socket.on("tracking:attendance", function (payload) {
            if (payload.ruta_id === rutaId) {
                updateAttendanceFeed(payload);
            }
        });
        socket.on("chat:new_message", function (payload) {
            if (payload.ruta_id === rutaId) {
                appendChatMessage(document.getElementById("chatMessages"), payload.remitente_nombre, payload.contenido);
            }
        });
        socket.on("tracking:plan_updated", function (payload) {
            if (payload.ruta_id === rutaId) {
                loadRoutePlan(mapContext, rutaId, pin);
            }
        });

        // Respaldo para que el portal de familias no se quede atras si el
        // navegador pierde algun evento en vivo.
        window.setInterval(function () {
            loadRouteHistory(mapContext, rutaId, pin);
            refreshPlannedRoute(mapContext);
        }, 8000);
    }

    function readJsonScript(elementId) {
        const node = document.getElementById(elementId);
        if (!node) {
            return null;
        }
        try {
            return JSON.parse(node.textContent);
        } catch (error) {
            window.console.error("No fue posible leer los datos embebidos del mapa.", error);
            return null;
        }
    }

    function updateAdminRouteMeta(payload) {
        if (!payload) {
            return;
        }
        setTextContent("adminRouteUpdatedAt", formatTimestamp(payload.ultima_ubicacion_en));
        const lat = parseFloat(payload.latitud);
        const lng = parseFloat(payload.longitud);
        setTextContent(
            "adminRouteCoordinates",
            Number.isNaN(lat) || Number.isNaN(lng) ? "Sin transmision" : `${lat.toFixed(5)}, ${lng.toFixed(5)}`
        );
    }

    function updateAdminRouteStatus(payload) {
        const node = document.getElementById("adminRouteState");
        if (!node || !payload) {
            return;
        }
        const activa = Boolean(payload.recorrido_activo);
        node.textContent = activa ? "En recorrido" : "En espera";
        node.classList.toggle("status-pill--ok", activa);
        node.classList.toggle("status-pill--off", !activa);
    }

    function loadAdminRoutePlan(mapContext, rutaId) {
        fetch(`/admin/api/ruta/${rutaId}/seguimiento`)
            .then(function (response) {
                return response.ok ? response.json() : null;
            })
            .then(function (payload) {
                if (payload && payload.plan) {
                    renderRoutePlan(mapContext, payload.plan);
                    refreshPlannedRoute(mapContext);
                }
            })
            .catch(function (error) {
                window.console.error("No fue posible refrescar el plan de la ruta.", error);
            });
    }

    function setupAdminRouteMap() {
        const mapNode = document.getElementById("adminRouteMap");
        if (!mapNode) {
            return;
        }
        const mapContext = createMap(mapNode);
        if (!mapContext) {
            return;
        }
        appState.adminMapContext = mapContext;

        const plan = readJsonScript("adminRoutePlanData");
        const puntos = readJsonScript("adminRouteHistoryData");
        const tracking = readJsonScript("adminRouteTrackingData");
        if (plan) {
            renderRoutePlan(mapContext, plan);
        }
        if (Array.isArray(puntos) && puntos.length) {
            setRouteHistory(mapContext, puntos);
            appState.adminTracePoints = puntos.length;
        }
        refreshPlannedRoute(mapContext);
        if (tracking && tracking.latitud !== null && tracking.longitud !== null) {
            updateMapLocation(mapContext, tracking);
        }
        updateAdminRouteMeta(tracking);
        updateAdminRouteStatus(tracking);

        const selector = document.getElementById("adminRouteSelector");
        if (selector) {
            selector.addEventListener("change", function () {
                window.location.href = `${window.location.pathname}?ruta_id=${encodeURIComponent(selector.value)}`;
            });
        }

        const rutaId = parseInt(mapNode.dataset.rutaId, 10);
        const pin = mapNode.dataset.pin;
        if (!rutaId || !pin) {
            return;
        }

        const refrescar = function () {
            fetch(`/admin/api/ruta/${rutaId}/seguimiento`)
                .then(function (response) {
                    return response.ok ? response.json() : null;
                })
                .then(function (payload) {
                    if (!payload) {
                        return;
                    }
                    if (payload.tracking) {
                        updateMapLocation(mapContext, payload.tracking);
                        updateAdminRouteMeta(payload.tracking);
                        updateAdminRouteStatus(payload.tracking);
                    }
                    if (Array.isArray(payload.puntos) && payload.puntos.length !== appState.adminTracePoints) {
                        appState.adminTracePoints = payload.puntos.length;
                        setRouteHistory(mapContext, payload.puntos);
                    }
                })
                .catch(function () {
                    /* El sondeo es un respaldo del socket; se reintenta en el siguiente ciclo. */
                });
        };
        appState.adminPollId = window.setInterval(refrescar, 20000);

        if (!socket) {
            return;
        }
        socket.emit("room:join", { ruta_id: rutaId, pin: pin });
        socket.on("tracking:location", function (payload) {
            if (payload.ruta_id !== rutaId) {
                return;
            }
            updateMapLocation(mapContext, payload);
            updateAdminRouteMeta(payload);
        });
        socket.on("tracking:route_status", function (payload) {
            if (payload.ruta_id !== rutaId) {
                return;
            }
            updateAdminRouteStatus(payload);
            if (payload.location_reset) {
                resetMapLocation(mapContext);
                updateAdminRouteMeta({ latitud: null, longitud: null, ultima_ubicacion_en: null });
            }
        });
        socket.on("tracking:plan_updated", function (payload) {
            if (payload.ruta_id === rutaId) {
                loadAdminRoutePlan(mapContext, rutaId);
            }
        });
    }

    function setupPasswordToggles() {
        document.querySelectorAll("[data-password-toggle]").forEach(function (button) {
            const input = document.getElementById(button.getAttribute("data-password-toggle"));
            if (!input) {
                return;
            }
            const icon = button.querySelector("i");
            button.addEventListener("click", function () {
                const visible = input.type === "password";
                input.type = visible ? "text" : "password";
                button.setAttribute("aria-label", visible ? "Ocultar contrasena" : "Mostrar contrasena");
                button.setAttribute("title", visible ? "Ocultar contrasena" : "Mostrar contrasena");
                if (icon) {
                    icon.classList.toggle("bi-eye", !visible);
                    icon.classList.toggle("bi-eye-slash", visible);
                }
            });
        });
    }

    function registerServiceWorker() {
        if (!("serviceWorker" in navigator)) {
            return;
        }
        window.addEventListener("load", function () {
            navigator.serviceWorker.register("/sw.js").catch(function () {
                /* Sin service worker la app sigue funcionando en linea. */
            });
        });
    }

    function setupPwaInstall() {
        const containers = document.querySelectorAll("[data-pwa-install]");
        if (!containers.length) {
            return;
        }

        const isStandalone =
            window.matchMedia("(display-mode: standalone)").matches ||
            window.matchMedia("(display-mode: fullscreen)").matches ||
            window.navigator.standalone === true;
        if (isStandalone) {
            return;
        }

        const isIos = /iphone|ipad|ipod/i.test(window.navigator.userAgent);
        const isMobile = isIos || /android/i.test(window.navigator.userAgent);
        let installEvent = null;

        function reveal() {
            containers.forEach(function (container) {
                container.hidden = false;
            });
        }

        function showNote(container, message) {
            const note = container.querySelector("[data-pwa-install-note]");
            if (!note) {
                return;
            }
            note.textContent = message;
            note.hidden = false;
        }

        containers.forEach(function (container) {
            const button = container.querySelector("[data-pwa-install-button]");
            if (!button) {
                return;
            }
            button.addEventListener("click", function () {
                if (installEvent) {
                    installEvent.prompt();
                    installEvent.userChoice
                        .then(function (choice) {
                            if (choice && choice.outcome === "accepted") {
                                container.hidden = true;
                            }
                        })
                        .catch(function () {});
                    installEvent = null;
                    return;
                }
                if (isIos) {
                    showNote(container, "Abre el boton Compartir de Safari y elige Anadir a pantalla de inicio.");
                    return;
                }
                showNote(container, "Abre el menu del navegador y elige Instalar aplicacion o Anadir a pantalla de inicio.");
            });
        });

        window.addEventListener("beforeinstallprompt", function (event) {
            event.preventDefault();
            installEvent = event;
            reveal();
        });

        window.addEventListener("appinstalled", function () {
            containers.forEach(function (container) {
                showNote(container, "Aplicacion instalada. Busca el icono en tu pantalla de inicio.");
            });
        });

        // En iPhone nunca se dispara beforeinstallprompt; en el resto de moviles
        // se muestra igual para ofrecer la guia manual de instalacion.
        if (isMobile) {
            reveal();
        }
    }

    function setupChatForm() {
        const form = document.getElementById("chatForm");
        if (!form || !socket) {
            return;
        }
        form.addEventListener("submit", function (event) {
            event.preventDefault();
            const rutaId = parseInt(form.dataset.rutaId, 10);
            const pin = form.dataset.pin;
            const remitenteTipo = form.dataset.remitenteTipo || "padre";
            const sender = document.getElementById("chatSender");
            const message = document.getElementById("chatMessage");
            if (!sender.value.trim() || !message.value.trim()) {
                return;
            }
            socket.emit("chat:message", {
                ruta_id: rutaId,
                pin: pin,
                remitente_tipo: remitenteTipo,
                remitente_nombre: sender.value.trim(),
                contenido: message.value.trim(),
            });
            message.value = "";
        });
    }

    async function requestWakeLock() {
        if (!("wakeLock" in navigator)) {
            return;
        }
        try {
            appState.wakeLock = await navigator.wakeLock.request("screen");
        } catch (error) {
            setDriverNotice("No fue posible bloquear el apagado de pantalla en este navegador.");
        }
    }

    function releaseWakeLock() {
        if (appState.wakeLock && typeof appState.wakeLock.release === "function") {
            appState.wakeLock.release();
        }
        appState.wakeLock = null;
    }

    function stopLocationWatch() {
        if (appState.driverWatchId !== null && navigator.geolocation) {
            navigator.geolocation.clearWatch(appState.driverWatchId);
        }
        appState.driverWatchId = null;
        releaseWakeLock();
    }

    function locationErrorMessage(error) {
        if (!error) {
            return "No fue posible leer la ubicación. Inténtalo de nuevo.";
        }
        if (error.code === 1) {
            return "Debes permitir la ubicación para transmitir el GPS de la ruta.";
        }
        if (error.code === 2) {
            return "El teléfono no pudo determinar tu ubicación. Activa el GPS e inténtalo otra vez.";
        }
        if (error.code === 3) {
            return "El GPS tardó demasiado. Muévete a un lugar con mejor señal e inténtalo otra vez.";
        }
        return "No fue posible leer la ubicación. Inténtalo de nuevo.";
    }

    function resetMapLocation(mapContext) {
        if (!mapContext) {
            return;
        }
        mapContext.latestLocation = null;
        mapContext.hasLocation = false;
        mapContext.rawRoutePoints = [];
        mapContext.lastTraceSignature = null;
        mapContext.lastTraceRequestAt = 0;
        // Al iniciar una nueva ruta se reinicia el calculo del trazado y las marcas de paradas.
        mapContext.lastPlanRequestAt = 0;
        mapContext.lastOptimizationSignature = null;
        mapContext.optimizedOrigin = null;
        mapContext.optimizedUsingLiveLocation = false;
        mapContext.plannedRouteFitted = false;
        mapContext.plannedRouteUsesLiveLocation = false;
        mapContext.selectedStopId = null;
        resetStopProgress(mapContext);
        if (mapContext.marker) {
            mapContext.marker.setOpacity(0);
        }
        if (mapContext.routeOutline) {
            mapContext.routeOutline.setLatLngs([]);
        }
        if (mapContext.routeLine) {
            mapContext.routeLine.setLatLngs([]);
        }
        if (mapContext.centerButton) {
            mapContext.centerButton.disabled = true;
        }
        if (mapContext.inlineCenterButton) {
            mapContext.inlineCenterButton.disabled = true;
            mapContext.inlineCenterButton.classList.add("is-disabled");
        }
        // Redibuja la ruta sugerida hacia las recogidas justo despues del reinicio.
        refreshPlannedRoute(mapContext);
    }

    function isStaleGpsPosition(position) {
        if (!position || typeof position.timestamp !== "number") {
            return false;
        }
        return (Date.now() - position.timestamp) > 120000;
    }

    function emitDriverLocationUpdate(rutaId, position) {
        if (!position || !position.coords) {
            return false;
        }
        if (isStaleGpsPosition(position)) {
            setGpsStatus("warning", "Esperando GPS actual");
            setDriverNotice("El telefono devolvio una ubicacion anterior. Esperando una posicion actual del GPS.");
            return false;
        }
        const payload = {
            ruta_id: rutaId,
            pin: appState.routePin,
            lat: position.coords.latitude,
            lng: position.coords.longitude,
            accuracy: position.coords.accuracy,
            speed: position.coords.speed,
            heading: position.coords.heading,
            captured_at: position.timestamp,
        };
        socket.emit("location:update", payload);
        setGpsStatus("active", "GPS transmitiendo");
        setDriverNotice("Transmitiendo tu ubicación en vivo a las familias.");
        return true;
    }

    async function startDriverTracking(rutaId) {
        if (!socket || !socket.connected) {
            setGpsStatus("warning", "Reconectando GPS");
            setDriverNotice("Estamos reconectando la ruta. El GPS se activará cuando vuelva la conexión.");
            return;
        }
        if (!isSecureLocationContext()) {
            setGpsStatus("error", "Requiere HTTPS");
            setDriverNotice("Para compartir GPS en producción debes usar la app con HTTPS.");
            return;
        }
        if (!navigator.geolocation) {
            setGpsStatus("error", "GPS no disponible");
            setDriverNotice("Este dispositivo o navegador no permite compartir ubicación.");
            return;
        }
        if (appState.driverWatchId !== null) {
            return;
        }
        setGpsStatus("warning", "Solicitando ubicación");
        setDriverNotice("Permite la ubicación del teléfono para iniciar la transmisión.");
        await requestWakeLock();
        navigator.geolocation.getCurrentPosition(
            function (position) {
                emitDriverLocationUpdate(rutaId, position);
            },
            function () {
                setGpsStatus("warning", "Buscando ubicación");
                setDriverNotice("No llegó una lectura inmediata. Seguimos buscando una ubicación actual del GPS.");
            },
            {
                enableHighAccuracy: true,
                maximumAge: 0,
                timeout: 12000,
            }
        );
        appState.driverWatchId = navigator.geolocation.watchPosition(
            function (position) {
                emitDriverLocationUpdate(rutaId, position);
            },
            function (error) {
                const message = locationErrorMessage(error);
                setGpsStatus("error", "GPS requiere atención");
                setDriverNotice(message);
            },
            {
                enableHighAccuracy: true,
                maximumAge: 0,
                timeout: 10000,
            }
        );
    }

    function setupHousePlacementControls(mapContext) {
        const select = document.getElementById("housePlacementSelect");
        const button = document.getElementById("housePlacementButton");
        const hint = document.getElementById("housePlacementHint");
        if (!mapContext || !select || !button) {
            return;
        }

        let pendingStudentId = null;

        function cancelPlacement(message) {
            pendingStudentId = null;
            mapContext.map.getContainer().classList.remove("is-placing-house");
            button.classList.remove("is-active");
            button.innerHTML = '<i class="bi bi-geo-alt me-1"></i> Ubicar casita en el mapa';
            if (hint && message) {
                hint.textContent = message;
            }
        }

        button.addEventListener("click", function () {
            const option = select.options[select.selectedIndex];
            if (!select.value || !option) {
                if (pendingStudentId !== null) {
                    cancelPlacement("Modo de ubicacion cancelado.");
                    return;
                }
                if (hint) {
                    hint.textContent = "Selecciona primero un estudiante de la lista.";
                }
                return;
            }
            pendingStudentId = parseInt(select.value, 10);
            mapContext.map.getContainer().classList.add("is-placing-house");
            button.classList.add("is-active");
            button.innerHTML = '<i class="bi bi-geo-alt me-1"></i> Confirmar en el mapa';
            if (hint) {
                hint.textContent = `Toca el mapa en el punto exacto de la casa de ${option.dataset.nombre || "este estudiante"}.`;
            }
        });

        select.addEventListener("change", function () {
            if (pendingStudentId === null) {
                return;
            }
            const option = select.options[select.selectedIndex];
            if (!select.value || !option) {
                return;
            }
            pendingStudentId = parseInt(select.value, 10);
            if (hint) {
                hint.textContent = `Ahora ubica la casa de ${option.dataset.nombre || "este estudiante"} tocando el mapa.`;
            }
        });

        mapContext.map.on("click", function (event) {
            if (pendingStudentId === null) {
                return;
            }
            const estudianteId = pendingStudentId;
            cancelPlacement("Guardando la ubicacion de la casita...");
            saveHouseLocation(estudianteId, event.latlng.lat, event.latlng.lng);
        });
    }

    function renderDriverHud(mapContext) {
        if (!mapContext || !mapContext.isDriverMap) {
            return;
        }
        const list = document.getElementById("driverHudStops");
        if (!list) {
            return;
        }
        const orderedStops = getOrderedStops(mapContext);
        const remainingStops = getRemainingStops(mapContext);
        const nextStop = getSelectedStop(mapContext) || remainingStops[0] || null;

        list.innerHTML = "";
        if (!orderedStops.length) {
            const empty = document.createElement("li");
            empty.className = "driver-hud-empty";
            empty.textContent = "Aun no hay paradas georreferenciadas en esta ruta.";
            list.appendChild(empty);
        } else {
            orderedStops.forEach(function (stop, index) {
                const item = document.createElement("li");
                item.className = "driver-hud-stop";
                item.classList.toggle("driver-hud-stop--complete", Boolean(stop.completada));
                item.classList.toggle(
                    "driver-hud-stop--next",
                    Boolean(nextStop && nextStop.id === stop.id)
                );

                const badge = document.createElement("span");
                badge.className = "driver-hud-stop-index";
                badge.textContent = String(index + 1);

                const text = document.createElement("span");
                text.className = "driver-hud-stop-text";
                const name = document.createElement("span");
                name.className = "driver-hud-stop-name";
                name.textContent = stop.nombre || "Parada";
                text.appendChild(name);
                if (stop.direccion) {
                    const meta = document.createElement("span");
                    meta.className = "driver-hud-stop-meta";
                    meta.textContent = stop.direccion;
                    text.appendChild(meta);
                }

                item.appendChild(badge);
                item.appendChild(text);
                list.appendChild(item);
            });
        }

        let headline = "Ruta completada";
        const metaParts = [];
        const selectedStop = getSelectedStop(mapContext);
        if (nextStop) {
            headline = nextStop.nombre || "Parada";
            if (nextStop.direccion) {
                metaParts.push(nextStop.direccion);
            }
            if (mapContext.latestLocation) {
                metaParts.push(formatDistance(haversineMeters(mapContext.latestLocation, nextStop.coordenadas)));
            }
            metaParts.push(selectedStop ? "ruta fijada" : `${remainingStops.length} ${remainingStops.length === 1 ? "parada" : "paradas"}`);
        } else if (mapContext.destination) {
            headline = mapContext.destination.nombre;
            metaParts.push("Destino final");
        }
        setTextContent("driverHudNextStop", headline);
        setTextContent("driverHudNextMeta", metaParts.join(" · ") || "--");
    }

    function requestMapFullscreen(element) {
        const request = element.requestFullscreen || element.webkitRequestFullscreen || element.msRequestFullscreen;
        if (!request) {
            return;
        }
        try {
            const result = request.call(element);
            if (result && typeof result.catch === "function") {
                result.catch(function () {});
            }
        } catch (error) {
            /* La vista ampliada dentro de la pagina sigue funcionando. */
        }
    }

    function exitMapFullscreen() {
        if (!document.fullscreenElement && !document.webkitFullscreenElement) {
            return;
        }
        const exit = document.exitFullscreen || document.webkitExitFullscreen || document.msExitFullscreen;
        if (!exit) {
            return;
        }
        try {
            const result = exit.call(document);
            if (result && typeof result.catch === "function") {
                result.catch(function () {});
            }
        } catch (error) {
            /* sin accion */
        }
    }

    function setupDriverMapExpand(mapContext) {
        if (!mapContext || !mapContext.isDriverMap) {
            return;
        }
        const card = mapContext.map.getContainer().closest(".driver-map-card");
        const button = document.querySelector('.js-map-expand[data-map-target="driverMap"]');
        if (!card || !button) {
            return;
        }
        mapContext.hudExpanded = false;

        function applyExpandedState(expanded) {
            mapContext.hudExpanded = expanded;
            card.classList.toggle("is-expanded", expanded);
            mapContext.map.getContainer().classList.toggle("is-map-hud", expanded);
            document.body.classList.toggle("has-expanded-map", expanded);
            if (mapContext.map.zoomControl) {
                mapContext.map.zoomControl.setPosition(expanded ? "bottomright" : "topright");
            }
            button.setAttribute("aria-pressed", expanded ? "true" : "false");
            button.setAttribute("title", expanded ? "Reducir mapa" : "Ampliar mapa");
            const icon = button.querySelector("i");
            const label = button.querySelector(".js-map-expand-label");
            if (icon) {
                icon.className = expanded ? "bi bi-arrows-angle-contract me-2" : "bi bi-arrows-angle-expand me-2";
            }
            if (label) {
                label.textContent = expanded ? "Reducir mapa" : "Ampliar mapa";
            }
            window.setTimeout(function () {
                mapContext.map.invalidateSize();
                if (expanded) {
                    focusVehicleLocation();
                    renderDriverHud(mapContext);
                }
            }, 260);
        }

        button.addEventListener("click", function () {
            const expanded = !mapContext.hudExpanded;
            if (expanded) {
                requestMapFullscreen(card);
            } else {
                exitMapFullscreen();
            }
            applyExpandedState(expanded);
        });

        document.addEventListener("fullscreenchange", function () {
            if (!document.fullscreenElement && mapContext.hudExpanded) {
                applyExpandedState(false);
            }
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && mapContext.hudExpanded) {
                exitMapFullscreen();
                applyExpandedState(false);
            }
        });
    }

    function setupDriverControls() {
        const driverPanel = document.getElementById("driverPanel");
        if (!driverPanel) {
            return;
        }
        const rutaId = parseInt(driverPanel.dataset.rutaId, 10);
        const pin = driverPanel.dataset.pin;
        const recorridoActivo = driverPanel.dataset.recorridoActivo === "true";
        const mapContext = createMap(document.getElementById("driverMap"));
        appState.driverMapContext = mapContext;
        appState.driverRouteActive = recorridoActivo;
        mapContext.rutaId = rutaId;
        mapContext.pin = pin;
        mapContext.allowHouseEditing = true;
        loadRouteHistory(mapContext, rutaId, pin, function () {
            setDriverNotice("No fue posible cargar el trayecto guardado en el mapa.");
        });
        loadRoutePlan(mapContext, rutaId, pin, function () {
            setDriverNotice("No fue posible cargar las paradas georreferenciadas de la ruta.");
            setVoiceStatus("Sin datos");
        });
        setupHousePlacementControls(mapContext);
        setupDriverMapExpand(mapContext);
        if (!socket) {
            return;
        }
        joinRouteRoom(rutaId, pin);

        if (recorridoActivo) {
            startDriverTracking(rutaId);
        }

        document.querySelectorAll(".js-route-start").forEach(function (button) {
            button.addEventListener("click", function () {
                socket.emit("route:start", { ruta_id: parseInt(button.dataset.rutaId, 10), pin: pin });
                setGpsStatus("warning", "Activando ruta");
                setDriverNotice("Activando la ruta y preparando el GPS del teléfono.");
            });
        });

        document.querySelectorAll(".js-route-finish").forEach(function (button) {
            button.addEventListener("click", function () {
                stopLocationWatch();
                socket.emit("route:finish", { ruta_id: parseInt(button.dataset.rutaId, 10), pin: pin });
                setGpsStatus("ready", "GPS detenido");
                setDriverNotice("Ruta finalizada. La transmisión de ubicación fue detenida.");
            });
        });

        document.querySelectorAll(".js-attendance-btn").forEach(function (button) {
            button.addEventListener("click", function () {
                socket.emit("attendance:update", {
                    ruta_id: parseInt(button.dataset.rutaId, 10),
                    pin: pin,
                    estudiante_id: parseInt(button.dataset.estudianteId, 10),
                    estado: button.dataset.estado,
                });
            });
        });

        socket.on("tracking:location", function (payload) {
            if (payload.ruta_id !== rutaId) {
                return;
            }
            updateMapLocation(mapContext, payload);
            updateLocationFields(payload, "driverCoordinates", "driverLastUpdate");
        });

        socket.on("tracking:route_status", function (payload) {
            if (payload.ruta_id !== rutaId) {
                return;
            }
            updateRouteStatus(payload);
            if (payload.location_reset) {
                resetMapLocation(mapContext);
                updateLocationFields({ latitud: null, longitud: null, ultima_ubicacion_en: null }, "driverCoordinates", "driverLastUpdate");
            }
            if (payload.recorrido_activo) {
                setDriverNotice("Ruta iniciada. Preparando la ubicación en vivo.");
                startDriverTracking(rutaId);
            } else {
                stopLocationWatch();
                setGpsStatus("ready", "GPS detenido");
                setDriverNotice("Ruta en espera. El GPS no se está compartiendo.");
            }
        });

        socket.on("tracking:attendance", function (payload) {
            if (payload.ruta_id !== rutaId) {
                return;
            }
            updateAttendanceFeed(payload);
            const item = document.querySelector(`.attendance-item[data-student-id="${payload.estudiante_id}"] .js-attendance-status`);
            if (item) {
                item.textContent = payload.estado;
            }
        });

        socket.on("chat:new_message", function (payload) {
            if (payload.ruta_id === rutaId) {
                appendChatMessage(document.getElementById("chatMessages"), payload.remitente_nombre, payload.contenido);
            }
        });
        socket.on("tracking:plan_updated", function (payload) {
            if (payload.ruta_id === rutaId) {
                loadRoutePlan(mapContext, rutaId, pin, function () {
                    setDriverNotice("No fue posible refrescar las casitas y la ruta sugerida.");
                });
            }
        });
    }

    function setupSocketErrors() {
        if (!socket) {
            return;
        }
        socket.on("system:error", function (payload) {
            const message = payload && payload.message ? payload.message : "Ocurrio un error en tiempo real.";
            setGpsStatus("error", "Revisa el GPS");
            setDriverNotice(message);
            window.console.error(message);
        });

        socket.on("connect", function () {
            if (appState.routeId && appState.routePin) {
                joinRouteRoom(appState.routeId, appState.routePin);
            }
            if (appState.driverWatchId === null) {
                setGpsStatus("ready", "Conexión lista");
            }
            if (window.speechSynthesis) {
                setVoiceStatus("Lista");
            } else {
                setVoiceStatus("No disponible");
            }
            if (appState.driverRouteActive) {
                startDriverTracking(appState.routeId);
            }
        });

        socket.on("disconnect", function () {
            if (appState.routeId) {
                setGpsStatus("warning", "Reconectando GPS");
                setDriverNotice("Se perdió la conexión. El GPS se reanudará automáticamente al reconectar.");
            }
        });

        socket.on("room:joined", function (payload) {
            const tracking = payload && payload.tracking;
            if (!tracking || tracking.ruta_id !== appState.routeId) {
                return;
            }
            updateRouteStatus(tracking);
            updateLocationFields(tracking, "driverCoordinates", "driverLastUpdate");
            updateMapLocation(appState.driverMapContext, tracking);
        });
    }

    function setupHeroCarousel() {
        const carousel = document.querySelector("[data-hero-carousel]");
        if (!carousel || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
            return;
        }
        const slides = Array.from(carousel.querySelectorAll(".hero-carousel-image"));
        if (slides.length < 2) {
            return;
        }

        let activeIndex = slides.findIndex(function (slide) {
            return slide.classList.contains("is-active");
        });
        if (activeIndex < 0) {
            activeIndex = 0;
            slides[activeIndex].classList.add("is-active");
        }

        window.setInterval(function () {
            slides[activeIndex].classList.remove("is-active");
            activeIndex = (activeIndex + 1) % slides.length;
            slides[activeIndex].classList.add("is-active");
        }, 7000);
    }

    function normalizeCatalogKey(value) {
        return (value || "")
            .toString()
            .normalize("NFD")
            .replace(/[\u0300-\u036f]/g, "")
            .trim()
            .toLowerCase();
    }

    function parseSchoolCatalog() {
        const node = document.getElementById("schoolCatalogData");
        if (!node) {
            return null;
        }
        try {
            return JSON.parse(node.textContent || "{}");
        } catch (error) {
            window.console.error("No fue posible leer el catalogo de colegios.", error);
            return null;
        }
    }

    function buildSchoolOptions(catalog, cityValue) {
        if (!catalog) {
            return [];
        }
        const normalizedCity = normalizeCatalogKey(cityValue);
        if (normalizedCity) {
            const matchedEntry = Object.entries(catalog).find(function (entry) {
                return normalizeCatalogKey(entry[0]) === normalizedCity;
            });
            if (matchedEntry) {
                return matchedEntry[1] || [];
            }
        }
        return Object.values(catalog).flat();
    }

    function replaceDatalistOptions(datalist, options) {
        if (!datalist) {
            return;
        }
        datalist.innerHTML = "";
        Array.from(new Set(options)).sort(function (left, right) {
            return left.localeCompare(right, "es");
        }).forEach(function (optionValue) {
            const option = document.createElement("option");
            option.value = optionValue;
            datalist.appendChild(option);
        });
    }

    function replaceSelectOptions(select, options, selectedValue) {
        if (!select) {
            return;
        }

        const placeholderText = select.dataset.placeholder || "Selecciona una opcion";
        const normalizedSelected = normalizeCatalogKey(selectedValue);
        const uniqueOptions = Array.from(new Set(options)).sort(function (left, right) {
            return left.localeCompare(right, "es");
        });
        const hasSelectedInCatalog = normalizedSelected && uniqueOptions.some(function (optionValue) {
            return normalizeCatalogKey(optionValue) === normalizedSelected;
        });

        select.innerHTML = "";

        const placeholderOption = document.createElement("option");
        placeholderOption.value = "";
        placeholderOption.textContent = placeholderText;
        select.appendChild(placeholderOption);

        if (selectedValue && !hasSelectedInCatalog) {
            const manualOption = document.createElement("option");
            manualOption.value = selectedValue;
            manualOption.textContent = selectedValue + " (actual)";
            select.appendChild(manualOption);
        }

        uniqueOptions.forEach(function (optionValue) {
            const option = document.createElement("option");
            option.value = optionValue;
            option.textContent = optionValue;
            select.appendChild(option);
        });

        if (selectedValue) {
            select.value = selectedValue;
        }
    }

    function setupSchoolCatalogForms() {
        const catalog = parseSchoolCatalog();
        const datalist = document.getElementById("colegiosCatalogo");
        if (!catalog) {
            return;
        }

        document.querySelectorAll(".js-school-form").forEach(function (form) {
            const cityInput = form.querySelector(".js-city-input");
            const schoolInputs = form.querySelectorAll(".js-school-input");
            const schoolSelects = form.querySelectorAll(".js-school-select");

            const syncOptions = function () {
                const options = buildSchoolOptions(catalog, cityInput ? cityInput.value : "");
                replaceDatalistOptions(datalist, options);

                schoolSelects.forEach(function (schoolSelect) {
                    const selectedValue = schoolSelect.dataset.selected || schoolSelect.value || "";
                    replaceSelectOptions(schoolSelect, options, selectedValue);
                });
            };

            if (cityInput) {
                cityInput.addEventListener("input", syncOptions);
                cityInput.addEventListener("change", syncOptions);
            }

            schoolInputs.forEach(function (schoolInput) {
                schoolInput.addEventListener("focus", syncOptions);
                schoolInput.addEventListener("input", syncOptions);
            });

            schoolSelects.forEach(function (schoolSelect) {
                schoolSelect.addEventListener("change", function () {
                    schoolSelect.dataset.selected = schoolSelect.value || "";
                });
            });

            syncOptions();
        });
    }

    document.addEventListener("visibilitychange", function () {
        if (!document.hidden && appState.driverWatchId !== null) {
            requestWakeLock();
        }
    });

    window.addEventListener("beforeunload", function () {
        stopLocationWatch();
    });

    document.addEventListener("DOMContentLoaded", function () {
        if (window.speechSynthesis) {
            setVoiceStatus("Lista");
        } else {
            setVoiceStatus("No disponible");
        }
        registerServiceWorker();
        setupHeroCarousel();
        setupSocketErrors();
        setupTrackingPage();
        setupAdminRouteMap();
        setupPasswordToggles();
        setupPwaInstall();
        setupChatForm();
        setupDriverControls();
        setupSchoolCatalogForms();
    });
})();
