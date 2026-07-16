using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

namespace DigitalTwin.Line11
{
    /// <summary>
    /// Standalone runtime view for the Line11 hydraulic press dashboard.
    /// Copy this single script into a project that uses the same asset hierarchy;
    /// no prefab or manual scene reference is required.
    /// </summary>
    public sealed class Line11PressDetailController : MonoBehaviour
    {
        [Serializable]
        private sealed class HydraulicMetric
        {
            public string Id;
            public string DisplayName;
            public float FlowRate;
            public float Velocity;
            public float Pressure;

            public HydraulicMetric(
                string id,
                string displayName,
                float flowRate,
                float velocity,
                float pressure)
            {
                Id = id;
                DisplayName = displayName;
                FlowRate = flowRate;
                Velocity = velocity;
                Pressure = pressure;
            }
        }

        private static readonly Color CanvasClear = new Color(0f, 0f, 0f, 0f);
        private static readonly Color SidebarColor = new Color32(20, 24, 29, 238);
        private static readonly Color PanelColor = new Color32(25, 29, 34, 250);
        private static readonly Color HeaderColor = new Color32(36, 42, 49, 255);
        private static readonly Color RowColor = new Color32(43, 49, 56, 255);
        private static readonly Color AlternateRowColor = new Color32(38, 44, 51, 255);
        private static readonly Color TextColor = new Color32(235, 240, 244, 255);
        private static readonly Color MutedTextColor = new Color32(153, 166, 177, 255);
        private static readonly Color AccentColor = new Color32(27, 156, 196, 255);
        private static readonly Color SuccessColor = new Color32(71, 190, 133, 255);

        private readonly List<HydraulicMetric> metrics = new List<HydraulicMetric>();
        private readonly Dictionary<string, Text[]> valueLabels =
            new Dictionary<string, Text[]>(StringComparer.OrdinalIgnoreCase);

        private Transform line11Root;
        private Transform pressFrame;
        private GameObject detailPanel;
        private Camera sceneCamera;
        private Font uiFont;
        private bool initialized;
        private bool detailOpen;
        private bool guiStylesReady;
        private bool guiRenderedLogged;
        private Rect sidebarGuiRect;
        private Rect line11ButtonGuiRect;
        private Rect detailGuiRect;
        private GUIStyle smallHeaderStyle;
        private GUIStyle titleStyle;
        private GUIStyle subtitleStyle;
        private GUIStyle lineButtonStyle;
        private GUIStyle closeButtonStyle;
        private GUIStyle tableHeaderStyle;
        private GUIStyle componentStyle;
        private GUIStyle valueStyle;
        private GUIStyle pressureStyle;
        private GUIStyle footnoteStyle;
        private GUIStyle bodyStyle;
        private GUIStyle pipeButtonStyle;
        private Texture2D buttonNormalTexture;
        private Texture2D buttonHoverTexture;
        private Texture2D buttonActiveTexture;
        private readonly float[] flowHistory = new float[24];
        private readonly float[] velocityHistory = new float[24];
        private readonly float[] pressureHistory = new float[24];
        private float lastHistoryUpdate;
        private int selectedPipeIndex;
        private int warningAlarmCount = 2;
        private float targetPressureBar = 116f;
        private float reliefSetPressureBar = 130f;
        private Vector2 metricsScrollPosition;
        private Camera assetPreviewCamera;
        private RenderTexture assetPreviewTexture;
        private GameObject assetPreviewClone;
        private Animator assetPreviewAnimator;
        private Transform assetPreviewShaft;
        private Vector3 shaftRetractedPosition;
        private Vector3 shaftPressedPosition;
        private int originalSceneCameraCullingMask;
        private bool sceneCameraMaskModified;

        private const int AssetPreviewLayer = 31;

        public static Line11PressDetailController Instance { get; private set; }

        public bool IsOpen => detailOpen;

        private void Awake()
        {
            if (Instance != null && Instance != this)
            {
                Destroy(gameObject);
                return;
            }

            Instance = this;
        }

        private void Start()
        {
            if (!initialized)
                Initialize();
        }

        public void Initialize(Transform targetLine11Root = null)
        {
            if (initialized)
            {
                if (targetLine11Root != null && line11Root == null)
                    BindLine11(targetLine11Root);

                return;
            }

            initialized = true;
            Instance = this;
            sceneCamera = Camera.main;

            if (sceneCamera == null)
                sceneCamera = FindAnyObjectByType<Camera>();

            CreateDefaultMetrics();
            InitializeRealtimeHistory();
            uiFont = CreateFont();

            if (targetLine11Root != null)
                BindLine11(targetLine11Root);
            else
                TryBindLine11();

            Debug.Log(
                line11Root != null
                    ? "[Line11 UI] Ready. Line11_Press and Hydraulic_Press_Frame are connected."
                    : "[Line11 UI] Ready. Waiting for Line11_Press to become available.");
        }

        /// <summary>
        /// Updates one row from a solver, PLC, MQTT client, or other live-data source.
        /// IDs: pipe-pump-tank, pipe-pump-valve, pipe-valve-tank-return,
        /// pipe-valve-tank-relief, motor, valve, cylinder.
        /// </summary>
        public void SetMetric(
            string componentId,
            float flowRate,
            float velocity,
            float pressure)
        {
            HydraulicMetric metric = metrics.Find(
                item => string.Equals(item.Id, componentId, StringComparison.OrdinalIgnoreCase));

            if (metric == null)
            {
                Debug.LogWarning($"[Line11 UI] Unknown metric id: {componentId}");
                return;
            }

            metric.FlowRate = flowRate;
            metric.Velocity = velocity;
            metric.Pressure = pressure;
            RefreshMetric(metric);
        }

        /// <summary>
        /// Updates the accumulated warning-event count shown in Condition Summary.
        /// </summary>
        public void SetWarningAlarmCount(int count)
        {
            warningAlarmCount = Mathf.Max(0, count);
        }

        /// <summary>
        /// Records one new warning event.
        /// </summary>
        public void RecordWarningAlarm()
        {
            warningAlarmCount++;
        }

        /// <summary>
        /// Updates the target operating pressure and configured relief-valve pressure.
        /// </summary>
        public void SetPressureTargets(float targetBar, float reliefSetBar)
        {
            targetPressureBar = Mathf.Max(0f, targetBar);
            reliefSetPressureBar = Mathf.Max(0f, reliefSetBar);
        }

        public void OpenDetail()
        {
            detailOpen = true;
        }

        public void CloseDetail()
        {
            detailOpen = false;
        }

        private void OnDestroy()
        {
            if (Instance == this)
                Instance = null;

            if (buttonNormalTexture != null)
                Destroy(buttonNormalTexture);
            if (buttonHoverTexture != null)
                Destroy(buttonHoverTexture);
            if (buttonActiveTexture != null)
                Destroy(buttonActiveTexture);

            if (sceneCameraMaskModified && sceneCamera != null)
                sceneCamera.cullingMask = originalSceneCameraCullingMask;

            if (assetPreviewCamera != null)
                Destroy(assetPreviewCamera.gameObject);
            if (assetPreviewClone != null)
                Destroy(assetPreviewClone);
            if (assetPreviewTexture != null)
            {
                assetPreviewTexture.Release();
                Destroy(assetPreviewTexture);
            }
        }

        private void Update()
        {
            if (!initialized)
                return;

            if (line11Root == null && Time.frameCount % 30 == 0)
                TryBindLine11();

            UpdateRealtimeHistory();
            UpdateAssetPreviewAnimation();

            if (IsOpen && Input.GetKeyDown(KeyCode.Escape))
                CloseDetail();

            if (!Input.GetMouseButtonDown(0))
                return;

            Vector2 guiMousePosition = new Vector2(
                Input.mousePosition.x,
                Screen.height - Input.mousePosition.y);

            if (sidebarGuiRect.Contains(guiMousePosition) ||
                (detailOpen && detailGuiRect.Contains(guiMousePosition)))
                return;

            TryOpenFromFrameClick();
        }

        private void TryOpenFromFrameClick()
        {
            if (sceneCamera == null)
                sceneCamera = Camera.main != null ? Camera.main : FindAnyObjectByType<Camera>();

            if (sceneCamera == null || pressFrame == null)
                return;

            Ray ray = sceneCamera.ScreenPointToRay(Input.mousePosition);
            RaycastHit[] hits = Physics.RaycastAll(
                ray,
                sceneCamera.farClipPlane,
                Physics.DefaultRaycastLayers,
                QueryTriggerInteraction.Ignore);

            foreach (RaycastHit hit in hits)
            {
                Transform hitTransform = hit.collider.transform;

                if (hitTransform == pressFrame || hitTransform.IsChildOf(pressFrame))
                {
                    OpenDetail();
                    return;
                }

                // The press prefab already owns a root collider. Accept it as well so the
                // visible frame remains clickable even when its imported mesh collider is occluded.
                if (hitTransform == line11Root || hitTransform.IsChildOf(line11Root))
                {
                    OpenDetail();
                    return;
                }
            }
        }

        private void OnGUI()
        {
            if (!initialized)
                return;

            EnsureGuiStyles();

            if (detailOpen)
                DrawDetailGui();

            DrawSidebarGui();

            if (!guiRenderedLogged && Event.current.type == EventType.Repaint)
            {
                guiRenderedLogged = true;
                Debug.Log($"[Line11 UI] IMGUI rendered. Screen: {Screen.width}x{Screen.height}");
            }
        }

        private void DrawSidebarGui()
        {
            float sidebarWidth = detailOpen ? 54f : 150f;
            sidebarGuiRect = new Rect(0f, 0f, sidebarWidth, Screen.height);
            DrawColorRect(sidebarGuiRect, SidebarColor);

            GUI.Label(
                new Rect(6f, 8f, sidebarWidth - 12f, 32f),
                detailOpen ? "DT" : "DIGITAL TWIN",
                detailOpen ? pressureStyle : smallHeaderStyle);

            line11ButtonGuiRect = new Rect(6f, 68f, sidebarWidth - 12f, 50f);
            if (GUI.Button(
                    line11ButtonGuiRect,
                    detailOpen ? "L11" : "Line11",
                    lineButtonStyle))
            {
                OpenDetail();
            }

            if (!detailOpen)
            {
                GUI.Label(
                    new Rect(10f, 126f, sidebarWidth - 20f, 26f),
                    "Hydraulic Press",
                    subtitleStyle);
            }
        }

        private void DrawDetailGui()
        {
            detailGuiRect = new Rect(0f, 0f, Screen.width, Screen.height);
            DrawColorRect(detailGuiRect, new Color32(20, 23, 26, 255));

            const float navWidth = 54f;
            const float topBarHeight = 46f;
            DrawColorRect(new Rect(navWidth, 0f, Screen.width - navWidth, topBarHeight), HeaderColor);

            GUI.Label(
                new Rect(navWidth + 14f, 5f, 380f, 34f),
                "<  LINE11 HYDRAULIC PRESS",
                smallHeaderStyle);
            GUI.Label(
                new Rect(Screen.width - 330f, 5f, 250f, 34f),
                "LIVE MONITORING   |   SYSTEM NORMAL",
                subtitleStyle);

            if (GUI.Button(
                    new Rect(Screen.width - 42f, 8f, 28f, 28f),
                    "X",
                    closeButtonStyle))
            {
                CloseDetail();
                return;
            }

            const float gap = 8f;
            float bodyX = navWidth + gap;
            float bodyY = topBarHeight + gap;
            float bodyWidth = Screen.width - bodyX - gap;
            float bodyHeight = Screen.height - bodyY - gap;
            float pressCycleWidth = bodyWidth * 0.24f;
            float schematicWidth = bodyWidth * 0.31f;
            float realtimeWidth = bodyWidth - pressCycleWidth - schematicWidth - gap * 2f;

            Rect pressCycleColumn = new Rect(bodyX, bodyY, pressCycleWidth, bodyHeight);
            Rect schematicColumn = new Rect(pressCycleColumn.xMax + gap, bodyY, schematicWidth, bodyHeight);
            Rect realtimeColumn = new Rect(schematicColumn.xMax + gap, bodyY, realtimeWidth, bodyHeight);

            DrawPressCycleColumn(pressCycleColumn);
            DrawSchematicAndMetrics(schematicColumn);
            DrawRealtimeColumn(realtimeColumn);
        }

        private void DrawPressCycleColumn(Rect column)
        {
            const float gap = 8f;
            float statusHeight = column.height * 0.20f;
            float cycleHeight = column.height * 0.51f;
            Rect statusPanel = new Rect(column.x, column.y, column.width, statusHeight);
            Rect cyclePanel = new Rect(column.x, statusPanel.yMax + gap, column.width, cycleHeight);
            Rect conditionPanel = new Rect(
                column.x,
                cyclePanel.yMax + gap,
                column.width,
                column.yMax - cyclePanel.yMax - gap);

            GetPressCycleState(out float strokeNormalized, out string stage, out float phase);
            DrawCycleStatusPanel(statusPanel, strokeNormalized, stage, phase);
            DrawRamPositionPanel(cyclePanel, strokeNormalized, stage);
            DrawConditionPanel(conditionPanel);
        }

        private void DrawCycleStatusPanel(
            Rect rect,
            float strokeNormalized,
            string stage,
            float phase)
        {
            DrawColorRect(rect, PanelColor);
            GUI.Label(
                new Rect(rect.x + 12f, rect.y + 6f, rect.width - 24f, 26f),
                "PRESS CYCLE OVERVIEW",
                smallHeaderStyle);

            Rect statusChip = new Rect(rect.x + 12f, rect.y + 38f, 78f, 26f);
            DrawColorRect(statusChip, SuccessColor);
            GUI.Label(statusChip, "RUNNING", tableHeaderStyle);
            GUI.Label(
                new Rect(statusChip.xMax + 10f, rect.y + 38f, rect.width - 112f, 26f),
                stage,
                pressureStyle);

            Rect progressTrack = new Rect(rect.x + 12f, rect.yMax - 24f, rect.width - 24f, 10f);
            DrawColorRect(progressTrack, HeaderColor);
            DrawColorRect(
                new Rect(progressTrack.x, progressTrack.y, progressTrack.width * phase, progressTrack.height),
                AccentColor);
            GUI.Label(
                new Rect(rect.x + 12f, progressTrack.y - 19f, rect.width - 24f, 18f),
                $"Cycle  {phase * 100f:0}%     Stroke  {strokeNormalized * 400f:0} / 400 mm",
                footnoteStyle);
        }

        private void DrawRamPositionPanel(Rect rect, float strokeNormalized, string stage)
        {
            DrawColorRect(rect, PanelColor);
            GUI.Label(
                new Rect(rect.x + 12f, rect.y + 6f, rect.width - 24f, 26f),
                "RAM POSITION / PRESS FORCE",
                smallHeaderStyle);

            Rect previewRect = new Rect(
                rect.x + 10f,
                rect.y + 36f,
                rect.width - 20f,
                Mathf.Max(62f, rect.height - 116f));
            DrawColorRect(previewRect, new Color32(31, 35, 39, 255));
            if (assetPreviewTexture != null)
                GUI.DrawTexture(previewRect, assetPreviewTexture, ScaleMode.ScaleAndCrop, false);
            else
                GUI.Label(previewRect, "PRESS FRAME ASSET", tableHeaderStyle);

            Rect liveBadge = new Rect(previewRect.x + 6f, previewRect.y + 6f, 70f, 20f);
            DrawColorRect(liveBadge, new Color32(27, 156, 196, 215));
            GUI.Label(liveBadge, "LIVE ASSET", tableHeaderStyle);

            float currentPressure = pressureHistory[pressureHistory.Length - 1];
            float pressForce = currentPressure * 0.82f;
            float cardGap = 5f;
            float cardY = previewRect.yMax + 5f;
            float cardHeight = Mathf.Max(34f, rect.yMax - 34f - cardY);
            float cardWidth = (rect.width - 20f - cardGap * 2f) / 3f;
            float cardX = rect.x + 10f;

            DrawCycleValueCard(
                new Rect(cardX, cardY, cardWidth, cardHeight),
                "RAM POSITION",
                $"{strokeNormalized * 400f:0} mm",
                AccentColor);
            cardX += cardWidth + cardGap;
            DrawCycleValueCard(
                new Rect(cardX, cardY, cardWidth, cardHeight),
                "PRESSURE",
                $"{currentPressure:0.0} bar",
                TextColor);
            cardX += cardWidth + cardGap;
            DrawCycleValueCard(
                new Rect(cardX, cardY, cardWidth, cardHeight),
                "PRESS FORCE",
                $"{pressForce:0.0} ton",
                new Color32(224, 172, 69, 255));

            GUI.Label(
                new Rect(rect.x + 12f, rect.yMax - 34f, rect.width - 24f, 22f),
                $"CURRENT STAGE   {stage}",
                footnoteStyle);
        }

        private void DrawCycleValueCard(Rect rect, string label, string value, Color accent)
        {
            DrawColorRect(rect, HeaderColor);
            DrawColorRect(new Rect(rect.x, rect.y, 3f, rect.height), accent);
            float labelHeight = Mathf.Min(18f, rect.height * 0.38f);
            GUI.Label(new Rect(rect.x + 7f, rect.y + 2f, rect.width - 10f, labelHeight), label, footnoteStyle);
            GUI.Label(
                new Rect(rect.x + 7f, rect.y + labelHeight, rect.width - 10f, rect.height - labelHeight - 2f),
                value,
                valueStyle);
        }

        private void DrawConditionPanel(Rect rect)
        {
            DrawColorRect(rect, PanelColor);
            GUI.Label(
                new Rect(rect.x + 12f, rect.y + 6f, rect.width - 24f, 26f),
                "CONDITION SUMMARY",
                smallHeaderStyle);

            float gap = 6f;
            float cardWidth = (rect.width - 30f) * 0.5f;
            float cardHeight = Mathf.Max(38f, (rect.height - 46f - gap) * 0.5f);
            float firstX = rect.x + 12f;
            float secondX = firstX + cardWidth + gap;
            float firstY = rect.y + 34f;
            float secondY = firstY + cardHeight + gap;

            DrawConditionCard(new Rect(firstX, firstY, cardWidth, cardHeight),
                "OIL TEMP", "44.2 C", SuccessColor);
            DrawConditionCard(new Rect(secondX, firstY, cardWidth, cardHeight),
                "PUMP RPM", "1450 RPM", AccentColor);
            DrawConditionCard(new Rect(firstX, secondY, cardWidth, cardHeight),
                "ASSET HEALTH", "92 %", SuccessColor);
            DrawConditionCard(new Rect(secondX, secondY, cardWidth, cardHeight),
                "WARNING", $"{warningAlarmCount} EVENTS", new Color32(216, 110, 76, 255));
        }

        private void DrawConditionCard(Rect rect, string label, string value, Color accent)
        {
            DrawColorRect(rect, HeaderColor);
            GUI.Label(new Rect(rect.x + 7f, rect.y + 2f, rect.width - 12f, 18f), label, footnoteStyle);
            GUI.Label(new Rect(rect.x + 7f, rect.y + 17f, rect.width - 12f, rect.height - 19f),
                value, valueStyle);
            DrawColorRect(new Rect(rect.x, rect.yMax - 3f, rect.width, 3f), accent);
        }

        private static void GetPressCycleState(
            out float strokeNormalized,
            out string stage,
            out float cyclePhase)
        {
            cyclePhase = Mathf.Repeat(Time.unscaledTime / 8f, 1f);

            if (cyclePhase < 0.15f)
            {
                stage = "LOADING";
                strokeNormalized = 0f;
            }
            else if (cyclePhase < 0.55f)
            {
                stage = "PRESSING";
                strokeNormalized = Mathf.InverseLerp(0.15f, 0.55f, cyclePhase);
            }
            else if (cyclePhase < 0.70f)
            {
                stage = "HOLDING";
                strokeNormalized = 1f;
            }
            else
            {
                stage = "RETURNING";
                strokeNormalized = 1f - Mathf.InverseLerp(0.70f, 1f, cyclePhase);
            }
        }

        private void DrawSchematicAndMetrics(Rect panel)
        {
            DrawColorRect(panel, PanelColor);
            GUI.Label(
                new Rect(panel.x + 12f, panel.y + 6f, panel.width - 24f, 28f),
                "PRESSURE SETTINGS / COMPONENT METRICS",
                smallHeaderStyle);

            const float summaryHeight = 92f;
            Rect pressureSummary = new Rect(panel.x + 10f, panel.y + 38f, panel.width - 20f, summaryHeight);
            DrawPressAssetPreview(pressureSummary);

            float tableY = pressureSummary.yMax + 8f;
            float tableHeight = panel.yMax - tableY - 10f;
            DrawMetricTable(new Rect(panel.x + 10f, tableY, panel.width - 20f, tableHeight));
        }

        private void DrawPressAssetPreview(Rect rect)
        {
            DrawColorRect(rect, new Color32(31, 35, 39, 255));
            GUI.Label(new Rect(rect.x + 10f, rect.y + 4f, rect.width - 20f, 22f),
                "PRESSURE SETPOINTS", footnoteStyle);

            const float gap = 8f;
            float cardWidth = (rect.width - 20f - gap) * 0.5f;
            Rect targetCard = new Rect(rect.x + 10f, rect.y + 28f, cardWidth, 54f);
            Rect reliefCard = new Rect(targetCard.xMax + gap, targetCard.y, cardWidth, targetCard.height);
            DrawPressureSettingCard(targetCard, "TARGET PRESSURE", targetPressureBar, AccentColor);
            DrawPressureSettingCard(reliefCard, "RELIEF SET PRESSURE", reliefSetPressureBar,
                new Color32(224, 172, 69, 255));
        }

        private void DrawPressureSettingCard(Rect rect, string label, float value, Color accent)
        {
            DrawColorRect(rect, HeaderColor);
            DrawColorRect(new Rect(rect.x, rect.y, 3f, rect.height), accent);
            GUI.Label(new Rect(rect.x + 10f, rect.y + 3f, rect.width - 16f, 18f), label, footnoteStyle);
            GUI.Label(new Rect(rect.x + 10f, rect.y + 20f, rect.width - 16f, 30f),
                $"{value:0.0} bar", valueStyle);
        }

        private void DrawMetricTable(Rect rect)
        {
            float componentWidth = rect.width * 0.50f;
            float valueWidth = (rect.width - componentWidth) / 3f;
            const float headerHeight = 36f;

            DrawColorRect(new Rect(rect.x, rect.y, rect.width, headerHeight), HeaderColor);
            GUI.Label(new Rect(rect.x + 6f, rect.y, componentWidth - 6f, headerHeight),
                "COMPONENT", tableHeaderStyle);
            GUI.Label(new Rect(rect.x + componentWidth, rect.y, valueWidth, headerHeight),
                "FLOW\nL/min", tableHeaderStyle);
            GUI.Label(new Rect(rect.x + componentWidth + valueWidth, rect.y, valueWidth, headerHeight),
                "SPEED\nm/s", tableHeaderStyle);
            GUI.Label(new Rect(rect.x + componentWidth + valueWidth * 2f, rect.y, valueWidth, headerHeight),
                "PRESS.\nbar", tableHeaderStyle);

            const float gap = 4f;
            const float rowHeight = 40f;
            Rect viewport = new Rect(rect.x, rect.y + headerHeight + 5f, rect.width,
                rect.height - headerHeight - 5f);
            float contentHeight = metrics.Count * (rowHeight + gap) - gap;
            float scrollbarReserve = contentHeight > viewport.height ? 18f : 2f;
            float contentWidth = Mathf.Max(100f, viewport.width - scrollbarReserve);
            Rect content = new Rect(0f, 0f, contentWidth, contentHeight);

            metricsScrollPosition = GUI.BeginScrollView(
                viewport,
                metricsScrollPosition,
                content,
                false,
                false);

            componentWidth = contentWidth * 0.50f;
            valueWidth = (contentWidth - componentWidth) / 3f;
            float y = 0f;

            for (int index = 0; index < metrics.Count; index++)
            {
                HydraulicMetric metric = metrics[index];
                DrawColorRect(
                    new Rect(0f, y, contentWidth, rowHeight),
                    index % 2 == 0 ? RowColor : AlternateRowColor);
                GUI.Label(new Rect(6f, y, componentWidth - 8f, rowHeight),
                    metric.DisplayName, componentStyle);
                GUI.Label(new Rect(componentWidth, y, valueWidth, rowHeight),
                    FormatValue(metric.FlowRate), valueStyle);
                GUI.Label(new Rect(componentWidth + valueWidth, y, valueWidth, rowHeight),
                    FormatValue(metric.Velocity), valueStyle);
                GUI.Label(new Rect(componentWidth + valueWidth * 2f, y, valueWidth, rowHeight),
                    FormatValue(metric.Pressure), pressureStyle);
                y += rowHeight + gap;
            }

            GUI.EndScrollView();
        }

        private void DrawAssetColumn(Rect column)
        {
            const float gap = 8f;
            float gaugeHeight = column.height * 0.31f;
            float profileHeight = column.height * 0.22f;
            Rect gauge = new Rect(column.x, column.y, column.width, gaugeHeight);
            Rect profile = new Rect(column.x, gauge.yMax + gap, column.width, profileHeight);
            Rect safety = new Rect(column.x, profile.yMax + gap, column.width,
                column.yMax - profile.yMax - gap);

            DrawGaugePanel(gauge);
            DrawProfilePanel(profile);
            DrawSafetyPanel(safety);
        }

        private void DrawGaugePanel(Rect rect)
        {
            DrawColorRect(rect, PanelColor);
            GUI.Label(new Rect(rect.x + 12f, rect.y + 6f, rect.width - 24f, 26f),
                "ASSET METRICS", smallHeaderStyle);

            float utilization = Mathf.Clamp01(pressureHistory[pressureHistory.Length - 1] / 160f);
            Vector2 center = new Vector2(rect.center.x, rect.y + rect.height * 0.68f);
            float radius = Mathf.Min(rect.width * 0.28f, rect.height * 0.32f);
            const int segmentCount = 22;

            for (int index = 0; index < segmentCount; index++)
            {
                float t0 = index / (float)segmentCount;
                float t1 = (index + 0.72f) / segmentCount;
                float angle0 = Mathf.Lerp(200f, 340f, t0) * Mathf.Deg2Rad;
                float angle1 = Mathf.Lerp(200f, 340f, t1) * Mathf.Deg2Rad;
                Color color = t0 < 0.65f
                    ? SuccessColor
                    : t0 < 0.85f
                        ? (Color)new Color32(224, 172, 69, 255)
                        : (Color)new Color32(216, 110, 76, 255);
                DrawLine(
                    center + new Vector2(Mathf.Cos(angle0), Mathf.Sin(angle0)) * radius,
                    center + new Vector2(Mathf.Cos(angle1), Mathf.Sin(angle1)) * radius,
                    color,
                    5f);
            }

            float needleAngle = Mathf.Lerp(200f, 340f, utilization) * Mathf.Deg2Rad;
            DrawLine(center, center + new Vector2(Mathf.Cos(needleAngle), Mathf.Sin(needleAngle)) * radius * 0.78f,
                TextColor, 2f);
            DrawColorRect(new Rect(center.x - 3f, center.y - 3f, 6f, 6f), TextColor);

            GUI.Label(new Rect(rect.x, rect.y + rect.height - 54f, rect.width, 28f),
                $"{utilization * 100f:0}%", pressureStyle);
            GUI.Label(new Rect(rect.x, rect.y + rect.height - 29f, rect.width, 20f),
                "Effective Utilization", tableHeaderStyle);
        }

        private void DrawProfilePanel(Rect rect)
        {
            DrawColorRect(rect, PanelColor);
            GUI.Label(new Rect(rect.x + 12f, rect.y + 6f, rect.width - 24f, 26f),
                "PRESSURE PROFILE", smallHeaderStyle);

            Rect bar = new Rect(rect.x + 14f, rect.y + rect.height * 0.48f, rect.width - 28f, 40f);
            DrawColorRect(new Rect(bar.x, bar.y, bar.width * 0.18f, bar.height), new Color32(147, 93, 182, 255));
            DrawColorRect(new Rect(bar.x + bar.width * 0.18f, bar.y, bar.width * 0.26f, bar.height), AccentColor);
            DrawColorRect(new Rect(bar.x + bar.width * 0.44f, bar.y, bar.width * 0.56f, bar.height), new Color32(66, 105, 190, 255));
            GUI.Label(bar, "12 bar       48 bar                  116 bar", tableHeaderStyle);
        }

        private void DrawSafetyPanel(Rect rect)
        {
            DrawColorRect(rect, PanelColor);
            GUI.Label(new Rect(rect.x + 12f, rect.y + 6f, rect.width - 24f, 26f),
                "OPERATIONAL SAFETY INTELLIGENCE", smallHeaderStyle);
            GUI.Label(new Rect(rect.x + 12f, rect.y + 38f, rect.width - 24f, 42f),
                "Hazard Description\nHigh hydraulic pressure may cause hot surfaces or fluid leakage.", bodyStyle);
            GUI.Label(new Rect(rect.x + 12f, rect.y + 90f, rect.width - 24f, 54f),
                "Control Measure\nWear eye protection, isolate pressure before maintenance, and inspect seals.", bodyStyle);

            float indicatorY = rect.yMax - 34f;
            DrawColorRect(new Rect(rect.x + 14f, indicatorY + 4f, 12f, 12f), SuccessColor);
            GUI.Label(new Rect(rect.x + 34f, indicatorY, rect.width - 48f, 22f),
                "Probability     LOW", tableHeaderStyle);
        }

        private void DrawRealtimeColumn(Rect column)
        {
            const float gap = 8f;
            float realtimeHeight = column.height * 0.64f;
            Rect realtime = new Rect(column.x, column.y, column.width, realtimeHeight);
            Rect recommendations = new Rect(column.x, realtime.yMax + gap, column.width,
                column.yMax - realtime.yMax - gap);

            DrawColorRect(realtime, PanelColor);
            GUI.Label(new Rect(realtime.x + 12f, realtime.y + 6f, realtime.width - 24f, 26f),
                "REAL-TIME VIEW", smallHeaderStyle);

            DrawPipeSelector(realtime);

            float chartGap = 6f;
            float chartStartY = realtime.y + 68f;
            float chartHeight = (realtime.yMax - chartStartY - chartGap * 2f - 8f) / 3f;
            DrawChart(new Rect(realtime.x + 10f, chartStartY, realtime.width - 20f, chartHeight),
                "FLOW RATE", flowHistory, 0f, 55f, "L/min", AccentColor);
            DrawChart(new Rect(realtime.x + 10f, chartStartY + chartHeight + chartGap,
                    realtime.width - 20f, chartHeight),
                "VELOCITY", velocityHistory, 0f, 4f, "m/s", new Color32(35, 181, 224, 255));
            DrawChart(new Rect(realtime.x + 10f, chartStartY + (chartHeight + chartGap) * 2f,
                    realtime.width - 20f, chartHeight),
                "PRESSURE", pressureHistory, 0f, 160f, "bar", new Color32(85, 141, 212, 255));

            DrawRecommendations(recommendations);
        }

        private void DrawPipeSelector(Rect realtimePanel)
        {
            string[] buttonLabels = { "P1  TANK", "P2  VALVE", "P3  RETURN", "P4  RELIEF" };
            GUIStyle selectorStyle = pipeButtonStyle ?? GUI.skin.button;
            float gap = 4f;
            float availableWidth = realtimePanel.width - 20f;
            float buttonWidth = (availableWidth - gap * (buttonLabels.Length - 1)) / buttonLabels.Length;
            float x = realtimePanel.x + 10f;
            float y = realtimePanel.y + 34f;

            for (int index = 0; index < buttonLabels.Length; index++)
            {
                Rect buttonRect = new Rect(x, y, buttonWidth, 27f);
                if (GUI.Button(buttonRect, buttonLabels[index], selectorStyle))
                    SelectPipe(index);

                if (index == selectedPipeIndex)
                    DrawColorRect(new Rect(buttonRect.x, buttonRect.yMax - 3f, buttonRect.width, 3f), AccentColor);

                x += buttonWidth + gap;
            }
        }

        private void SelectPipe(int pipeIndex)
        {
            int clampedIndex = Mathf.Clamp(pipeIndex, 0, Mathf.Min(3, metrics.Count - 1));
            if (selectedPipeIndex == clampedIndex)
                return;

            selectedPipeIndex = clampedIndex;
            InitializeRealtimeHistory();
        }

        private void DrawChart(
            Rect rect,
            string chartTitle,
            float[] values,
            float minimum,
            float maximum,
            string unit,
            Color lineColor)
        {
            DrawColorRect(rect, new Color32(31, 35, 39, 255));
            GUI.Label(new Rect(rect.x + 8f, rect.y + 2f, 120f, 20f), chartTitle, footnoteStyle);
            GUI.Label(new Rect(rect.xMax - 110f, rect.y + 2f, 102f, 20f),
                $"{values[values.Length - 1]:0.0} {unit}", tableHeaderStyle);

            Rect graph = new Rect(rect.x + 8f, rect.y + 22f, rect.width - 16f, rect.height - 29f);
            for (int grid = 1; grid < 4; grid++)
            {
                float gx = graph.x + graph.width * grid / 4f;
                DrawColorRect(new Rect(gx, graph.y, 1f, graph.height), new Color32(78, 84, 89, 110));
            }
            for (int grid = 1; grid < 3; grid++)
            {
                float gy = graph.y + graph.height * grid / 3f;
                DrawColorRect(new Rect(graph.x, gy, graph.width, 1f), new Color32(78, 84, 89, 110));
            }

            for (int index = 0; index < values.Length - 1; index++)
            {
                float x0 = graph.x + graph.width * index / (values.Length - 1f);
                float x1 = graph.x + graph.width * (index + 1) / (values.Length - 1f);
                float y0 = graph.yMax - Mathf.InverseLerp(minimum, maximum, values[index]) * graph.height;
                float y1 = graph.yMax - Mathf.InverseLerp(minimum, maximum, values[index + 1]) * graph.height;
                DrawLine(new Vector2(x0, y0), new Vector2(x1, y1), lineColor, 2f);
            }
        }

        private void DrawRecommendations(Rect rect)
        {
            DrawColorRect(rect, PanelColor);
            GUI.Label(new Rect(rect.x + 12f, rect.y + 6f, rect.width - 24f, 26f),
                "RECOMMENDATIONS / ALERTS", smallHeaderStyle);

            string[] alerts =
            {
                "P-778  Safety Warning: hydraulic pressure inspection required",
                "P-779  Check ValveToTank_Return flow stability",
                "P-780  Cylinder seal temperature within normal range"
            };

            float gap = 5f;
            float rowHeight = Mathf.Max(30f, (rect.height - 42f - gap * 2f) / 3f);
            float y = rect.y + 34f;
            for (int index = 0; index < alerts.Length; index++)
            {
                DrawColorRect(new Rect(rect.x + 10f, y, rect.width - 20f, rowHeight), RowColor);
                DrawColorRect(new Rect(rect.x + 10f, y, 4f, rowHeight),
                    index < 2 ? (Color)new Color32(216, 110, 76, 255) : SuccessColor);
                GUI.Label(new Rect(rect.x + 20f, y + 2f, rect.width - 34f, rowHeight - 4f),
                    alerts[index], bodyStyle);
                y += rowHeight + gap;
            }
        }

        private void InitializeRealtimeHistory()
        {
            HydraulicMetric selectedPipe = GetSelectedPipeMetric();
            float baseFlow = selectedPipe != null ? selectedPipe.FlowRate : 38f;
            float baseVelocity = selectedPipe != null ? selectedPipe.Velocity : 2.15f;
            float basePressure = selectedPipe != null ? selectedPipe.Pressure : 112f;

            for (int index = 0; index < flowHistory.Length; index++)
            {
                flowHistory[index] = baseFlow + Mathf.Sin(index * 0.62f) * Mathf.Max(0.6f, baseFlow * 0.08f);
                velocityHistory[index] = baseVelocity +
                    Mathf.Sin(index * 0.81f + 0.8f) * Mathf.Max(0.08f, baseVelocity * 0.12f);
                pressureHistory[index] = basePressure +
                    Mathf.Sin(index * 0.47f + 1.4f) * Mathf.Max(1f, basePressure * 0.06f);
            }
        }

        private void UpdateRealtimeHistory()
        {
            if (Time.unscaledTime - lastHistoryUpdate < 0.65f || metrics.Count == 0)
                return;

            lastHistoryUpdate = Time.unscaledTime;
            ShiftHistory(flowHistory);
            ShiftHistory(velocityHistory);
            ShiftHistory(pressureHistory);

            HydraulicMetric selectedPipe = GetSelectedPipeMetric();
            if (selectedPipe == null)
                return;

            float noise = Mathf.PerlinNoise(Time.unscaledTime * 0.18f, 0.4f) - 0.5f;
            flowHistory[flowHistory.Length - 1] =
                Mathf.Max(0f, selectedPipe.FlowRate + noise * Mathf.Max(1f, selectedPipe.FlowRate * 0.12f));
            velocityHistory[velocityHistory.Length - 1] =
                Mathf.Max(0f, selectedPipe.Velocity + noise * Mathf.Max(0.12f, selectedPipe.Velocity * 0.14f));
            pressureHistory[pressureHistory.Length - 1] =
                Mathf.Max(0f, selectedPipe.Pressure + noise * Mathf.Max(2f, selectedPipe.Pressure * 0.08f));
        }

        private HydraulicMetric GetSelectedPipeMetric()
        {
            if (metrics.Count == 0)
                return null;

            int index = Mathf.Clamp(selectedPipeIndex, 0, Mathf.Min(3, metrics.Count - 1));
            return metrics[index];
        }

        private static void ShiftHistory(float[] history)
        {
            for (int index = 0; index < history.Length - 1; index++)
                history[index] = history[index + 1];
        }

        private static void DrawLine(Vector2 start, Vector2 end, Color color, float width)
        {
            Vector2 delta = end - start;
            if (delta.sqrMagnitude < 0.01f)
                return;

            Matrix4x4 previousMatrix = GUI.matrix;
            float angle = Mathf.Atan2(delta.y, delta.x) * Mathf.Rad2Deg;
            GUIUtility.RotateAroundPivot(angle, start);
            DrawColorRect(new Rect(start.x, start.y, delta.magnitude, width), color);
            GUI.matrix = previousMatrix;
        }

        private void EnsureGuiStyles()
        {
            // Serialized runtime instances can survive a script hot reload with the old
            // ready flag but without styles that were added in the new script version.
            if (guiStylesReady && smallHeaderStyle != null && lineButtonStyle != null &&
                closeButtonStyle != null && pipeButtonStyle != null)
                return;

            guiStylesReady = true;
            buttonNormalTexture = CreateColorTexture(AccentColor);
            buttonHoverTexture = CreateColorTexture(new Color32(39, 178, 217, 255));
            buttonActiveTexture = CreateColorTexture(new Color32(18, 124, 160, 255));

            smallHeaderStyle = CreateGuiLabelStyle(14, FontStyle.Bold, TextColor, TextAnchor.MiddleLeft);
            titleStyle = CreateGuiLabelStyle(25, FontStyle.Bold, TextColor, TextAnchor.MiddleLeft);
            subtitleStyle = CreateGuiLabelStyle(14, FontStyle.Normal, MutedTextColor, TextAnchor.MiddleLeft);
            tableHeaderStyle = CreateGuiLabelStyle(12, FontStyle.Bold, MutedTextColor, TextAnchor.MiddleCenter);
            componentStyle = CreateGuiLabelStyle(14, FontStyle.Bold, TextColor, TextAnchor.MiddleLeft);
            valueStyle = CreateGuiLabelStyle(18, FontStyle.Bold, TextColor, TextAnchor.MiddleCenter);
            pressureStyle = CreateGuiLabelStyle(18, FontStyle.Bold, AccentColor, TextAnchor.MiddleCenter);
            footnoteStyle = CreateGuiLabelStyle(12, FontStyle.Italic, MutedTextColor, TextAnchor.MiddleLeft);
            bodyStyle = CreateGuiLabelStyle(12, FontStyle.Normal, TextColor, TextAnchor.UpperLeft);
            bodyStyle.wordWrap = true;

            lineButtonStyle = new GUIStyle(GUI.skin.button)
            {
                font = uiFont,
                fontSize = 20,
                fontStyle = FontStyle.Bold,
                alignment = TextAnchor.MiddleCenter
            };
            lineButtonStyle.normal.background = buttonNormalTexture;
            lineButtonStyle.hover.background = buttonHoverTexture;
            lineButtonStyle.active.background = buttonActiveTexture;
            lineButtonStyle.normal.textColor = Color.white;
            lineButtonStyle.hover.textColor = Color.white;
            lineButtonStyle.active.textColor = Color.white;

            closeButtonStyle = new GUIStyle(lineButtonStyle)
            {
                fontSize = 15
            };

            pipeButtonStyle = new GUIStyle(GUI.skin.button)
            {
                font = uiFont,
                fontSize = 11,
                fontStyle = FontStyle.Bold,
                alignment = TextAnchor.MiddleCenter
            };
            pipeButtonStyle.normal.background = buttonActiveTexture;
            pipeButtonStyle.hover.background = buttonHoverTexture;
            pipeButtonStyle.active.background = buttonNormalTexture;
            pipeButtonStyle.normal.textColor = TextColor;
            pipeButtonStyle.hover.textColor = Color.white;
            pipeButtonStyle.active.textColor = Color.white;
        }

        private GUIStyle CreateGuiLabelStyle(
            int fontSize,
            FontStyle fontStyle,
            Color color,
            TextAnchor alignment)
        {
            return new GUIStyle(GUI.skin.label)
            {
                font = uiFont,
                fontSize = fontSize,
                fontStyle = fontStyle,
                normal = { textColor = color },
                alignment = alignment,
                wordWrap = false,
                clipping = TextClipping.Clip
            };
        }

        private static void DrawColorRect(Rect rect, Color color)
        {
            Color previousColor = GUI.color;
            GUI.color = color;
            GUI.DrawTexture(rect, Texture2D.whiteTexture);
            GUI.color = previousColor;
        }

        private static Texture2D CreateColorTexture(Color color)
        {
            Texture2D texture = new Texture2D(1, 1, TextureFormat.RGBA32, false)
            {
                hideFlags = HideFlags.HideAndDontSave
            };
            texture.SetPixel(0, 0, color);
            texture.Apply();
            return texture;
        }

        private void PrepareFrameClickTarget()
        {
            pressFrame = FindDeepChild(line11Root, "Hydraulic_Press_Frame");

            if (pressFrame == null)
            {
                Debug.LogWarning("[Line11 UI] Hydraulic_Press_Frame was not found under Line11_Press.");
                return;
            }

            Collider existingCollider = pressFrame.GetComponent<Collider>();
            if (existingCollider != null)
            {
                return;
            }

            MeshFilter meshFilter = pressFrame.GetComponent<MeshFilter>();
            if (meshFilter == null || meshFilter.sharedMesh == null)
                return;

            MeshCollider frameCollider = pressFrame.gameObject.AddComponent<MeshCollider>();
            frameCollider.sharedMesh = meshFilter.sharedMesh;
        }

        private void TryBindLine11()
        {
            Transform target = FindLoadedSceneObject("Line11_Press");
            if (target != null)
                BindLine11(target);
        }

        private void BindLine11(Transform target)
        {
            line11Root = target;
            PrepareFrameClickTarget();
            SetupAssetPreview();

            if (pressFrame != null)
                Debug.Log($"[Line11 UI] Click target connected: {GetHierarchyPath(pressFrame)}");
        }

        private void SetupAssetPreview()
        {
            if (assetPreviewCamera != null || pressFrame == null)
                return;

            Transform sourceRoot = pressFrame.parent != null ? pressFrame.parent : pressFrame;
            assetPreviewClone = Instantiate(sourceRoot.gameObject);
            assetPreviewClone.name = "Line11 Hydraulic Press UI Preview";
            assetPreviewClone.hideFlags = HideFlags.HideAndDontSave;
            assetPreviewClone.transform.SetParent(transform, true);

            foreach (Collider previewCollider in assetPreviewClone.GetComponentsInChildren<Collider>(true))
                previewCollider.enabled = false;

            foreach (MonoBehaviour previewBehaviour in assetPreviewClone.GetComponentsInChildren<MonoBehaviour>(true))
                previewBehaviour.enabled = false;

            Transform previewRoot = assetPreviewClone.transform;
            Renderer[] renderers = previewRoot.GetComponentsInChildren<Renderer>(true);
            if (renderers.Length == 0)
            {
                Debug.LogWarning("[Line11 UI] No renderers were found for the hydraulic press preview.");
                Destroy(assetPreviewClone);
                assetPreviewClone = null;
                return;
            }

            Bounds bounds = renderers[0].bounds;
            foreach (Renderer renderer in renderers)
            {
                bounds.Encapsulate(renderer.bounds);
                renderer.gameObject.layer = AssetPreviewLayer;
            }

            Transform clonedFrame = FindDeepChild(previewRoot, "Hydraulic_Press_Frame");
            Renderer frameRenderer = clonedFrame != null ? clonedFrame.GetComponent<Renderer>() : null;
            Bounds focusBounds = frameRenderer != null ? frameRenderer.bounds : bounds;

            assetPreviewAnimator = assetPreviewClone.GetComponentInChildren<Animator>(true);
            if (assetPreviewAnimator != null)
                assetPreviewAnimator.enabled = false;

            assetPreviewShaft = FindDeepChild(previewRoot, "Hydraulic_Press_Shaft");
            if (assetPreviewShaft != null)
            {
                shaftRetractedPosition = assetPreviewShaft.localPosition;
                // The source clip moves about 0.218 local units. A slightly larger UI-only
                // travel makes the ram motion readable in the compact dashboard preview.
                shaftPressedPosition = shaftRetractedPosition + Vector3.down * 0.42f;
            }

            if (sceneCamera != null)
            {
                originalSceneCameraCullingMask = sceneCamera.cullingMask;
                sceneCamera.cullingMask &= ~(1 << AssetPreviewLayer);
                sceneCameraMaskModified = true;
            }

            assetPreviewTexture = new RenderTexture(640, 360, 24, RenderTextureFormat.ARGB32)
            {
                name = "Line11 Hydraulic Press Preview",
                hideFlags = HideFlags.HideAndDontSave,
                antiAliasing = 2,
                useMipMap = false,
                autoGenerateMips = false
            };
            assetPreviewTexture.Create();

            GameObject cameraObject = new GameObject("Line11 Asset Preview Camera", typeof(Camera));
            cameraObject.transform.SetParent(transform, false);
            assetPreviewCamera = cameraObject.GetComponent<Camera>();
            assetPreviewCamera.targetTexture = assetPreviewTexture;
            assetPreviewCamera.cullingMask = 1 << AssetPreviewLayer;
            assetPreviewCamera.clearFlags = CameraClearFlags.SolidColor;
            assetPreviewCamera.backgroundColor = new Color32(31, 35, 39, 255);
            assetPreviewCamera.orthographic = true;
            assetPreviewCamera.allowHDR = true;
            assetPreviewCamera.allowMSAA = true;
            assetPreviewCamera.depth = -100f;

            const float previewAspect = 640f / 360f;
            float horizontalSize = focusBounds.extents.x / previewAspect;
            assetPreviewCamera.orthographicSize =
                Mathf.Max(0.05f, Mathf.Max(focusBounds.extents.y, horizontalSize) * 1.04f);

            Transform viewTransform = clonedFrame != null ? clonedFrame : previewRoot;
            float distance = Mathf.Max(2f, focusBounds.extents.magnitude * 3f);
            Vector3 viewOffset =
                -viewTransform.forward * distance +
                viewTransform.right * distance * 0.20f +
                Vector3.up * distance * 0.12f;
            assetPreviewCamera.transform.position = focusBounds.center + viewOffset;
            assetPreviewCamera.transform.LookAt(
                focusBounds.center + Vector3.up * focusBounds.extents.y * 0.03f,
                Vector3.up);
            assetPreviewCamera.nearClipPlane = 0.05f;
            assetPreviewCamera.farClipPlane = distance * 5f;

            Debug.Log(
                $"[Line11 UI] Hydraulic press asset preview ready ({renderers.Length} renderers, " +
                $"focus {focusBounds.size}, ortho {assetPreviewCamera.orthographicSize:0.###}).");
        }

        private void UpdateAssetPreviewAnimation()
        {
            if (assetPreviewShaft == null)
                return;

            GetPressCycleState(out float strokeNormalized, out _, out _);
            float easedStroke = Mathf.SmoothStep(0f, 1f, strokeNormalized);
            assetPreviewShaft.localPosition = Vector3.Lerp(
                shaftRetractedPosition,
                shaftPressedPosition,
                easedStroke);
        }

        private void CreateDefaultMetrics()
        {
            // Temporary sample values. Replace these through SetMetric when live data is connected.
            metrics.Add(new HydraulicMetric(
                "pipe-pump-tank", "PIPE 01   Pump > Tank", 35.2f, 2.30f, 12.0f));
            metrics.Add(new HydraulicMetric(
                "pipe-pump-valve", "PIPE 02   Pump > Valve", 42.0f, 2.80f, 125.0f));
            metrics.Add(new HydraulicMetric(
                "pipe-valve-tank-return", "PIPE 03   Valve > Tank", 34.8f, 2.20f, 10.5f));
            metrics.Add(new HydraulicMetric(
                "pipe-valve-tank-relief", "PIPE 04   Relief > Tank", 4.1f, 0.45f, 126.0f));
            metrics.Add(new HydraulicMetric(
                "motor", "MOTOR   Drive Motor", 40.0f, 1.85f, 122.0f));
            metrics.Add(new HydraulicMetric(
                "valve", "VALVE   Directional", 39.1f, 2.60f, 120.0f));
            metrics.Add(new HydraulicMetric(
                "cylinder", "CYLINDER   Main", 37.8f, 0.42f, 116.0f));
        }

        private void BuildInterface()
        {
            uiFont = CreateFont();

            GameObject canvasObject = new GameObject(
                "Line11 Runtime UI",
                typeof(RectTransform),
                typeof(Canvas),
                typeof(CanvasScaler),
                typeof(GraphicRaycaster));
            canvasObject.transform.SetParent(transform, false);
            canvasObject.layer = LayerMask.NameToLayer("UI");

            Canvas canvas = canvasObject.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = 1000;

            CanvasScaler scaler = canvasObject.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1920f, 1080f);
            scaler.screenMatchMode = CanvasScaler.ScreenMatchMode.MatchWidthOrHeight;
            scaler.matchWidthOrHeight = 0.5f;

            Image canvasImage = canvasObject.AddComponent<Image>();
            canvasImage.color = CanvasClear;
            canvasImage.raycastTarget = false;

            BuildSidebar(canvasObject.transform);
            BuildDetailPanel(canvasObject.transform);
        }

        private void BuildSidebar(Transform canvasTransform)
        {
            GameObject sidebar = CreateImage("Line Navigation", canvasTransform, SidebarColor);
            RectTransform sidebarRect = sidebar.GetComponent<RectTransform>();
            sidebarRect.anchorMin = new Vector2(0f, 0f);
            sidebarRect.anchorMax = new Vector2(0f, 1f);
            sidebarRect.pivot = new Vector2(0f, 0.5f);
            sidebarRect.anchoredPosition = new Vector2(18f, 0f);
            sidebarRect.sizeDelta = new Vector2(170f, -36f);

            CreateLabel(
                "LINES",
                sidebar.transform,
                new Vector2(18f, -24f),
                new Vector2(134f, 28f),
                15,
                FontStyle.Bold,
                MutedTextColor,
                TextAnchor.MiddleLeft);

            Button line11Button = CreateButton(
                "Line11 Button",
                sidebar.transform,
                new Vector2(16f, -68f),
                new Vector2(138f, 54f),
                "Line11",
                20,
                AccentColor);
            line11Button.onClick.AddListener(OpenDetail);

            CreateLabel(
                "Hydraulic Press",
                sidebar.transform,
                new Vector2(18f, -130f),
                new Vector2(134f, 24f),
                13,
                FontStyle.Normal,
                MutedTextColor,
                TextAnchor.MiddleLeft);
        }

        private void BuildDetailPanel(Transform canvasTransform)
        {
            detailPanel = CreateImage("Line11 Detail Panel", canvasTransform, PanelColor);
            RectTransform panelRect = detailPanel.GetComponent<RectTransform>();
            panelRect.anchorMin = new Vector2(1f, 0.5f);
            panelRect.anchorMax = new Vector2(1f, 0.5f);
            panelRect.pivot = new Vector2(1f, 0.5f);
            panelRect.anchoredPosition = new Vector2(-24f, 0f);
            panelRect.sizeDelta = new Vector2(720f, 820f);

            CreateLabel(
                "LINE11 HYDRAULIC PRESS",
                detailPanel.transform,
                new Vector2(28f, -24f),
                new Vector2(470f, 38f),
                26,
                FontStyle.Bold,
                TextColor,
                TextAnchor.MiddleLeft);

            CreateLabel(
                "Flow rate, velocity and pressure by component",
                detailPanel.transform,
                new Vector2(30f, -64f),
                new Vector2(500f, 24f),
                15,
                FontStyle.Normal,
                MutedTextColor,
                TextAnchor.MiddleLeft);

            GameObject status = CreateImage("Status", detailPanel.transform, SuccessColor);
            SetTopLeftRect(status.GetComponent<RectTransform>(), new Vector2(544f, -35f), new Vector2(104f, 32f));
            CreateLabel(
                "LIVE SAMPLE",
                status.transform,
                Vector2.zero,
                new Vector2(104f, 32f),
                12,
                FontStyle.Bold,
                Color.white,
                TextAnchor.MiddleCenter);

            Button closeButton = CreateButton(
                "Close Button",
                detailPanel.transform,
                new Vector2(662f, -24f),
                new Vector2(34f, 34f),
                "X",
                17,
                HeaderColor);
            closeButton.onClick.AddListener(CloseDetail);

            GameObject divider = CreateImage("Header Divider", detailPanel.transform, AccentColor);
            SetTopLeftRect(divider.GetComponent<RectTransform>(), new Vector2(28f, -105f), new Vector2(664f, 2f));

            CreateLabel(
                "ASSET METRICS",
                detailPanel.transform,
                new Vector2(28f, -126f),
                new Vector2(220f, 28f),
                15,
                FontStyle.Bold,
                TextColor,
                TextAnchor.MiddleLeft);

            BuildTableHeader();
            BuildMetricRows();

            CreateLabel(
                "Temporary sample values. Connect live data with SetMetric(componentId, flow, velocity, pressure).",
                detailPanel.transform,
                new Vector2(28f, -770f),
                new Vector2(664f, 26f),
                13,
                FontStyle.Italic,
                MutedTextColor,
                TextAnchor.MiddleLeft);

            detailPanel.SetActive(false);
        }

        private void BuildTableHeader()
        {
            GameObject header = CreateImage("Metric Header", detailPanel.transform, HeaderColor);
            SetTopLeftRect(header.GetComponent<RectTransform>(), new Vector2(20f, -164f), new Vector2(680f, 48f));

            CreateTableLabel("COMPONENT", header.transform, 12f, 285f, TextAnchor.MiddleLeft);
            CreateTableLabel("FLOW RATE\nL/min", header.transform, 304f, 118f, TextAnchor.MiddleCenter);
            CreateTableLabel("VELOCITY\nm/s", header.transform, 426f, 112f, TextAnchor.MiddleCenter);
            CreateTableLabel("PRESSURE\nbar", header.transform, 542f, 126f, TextAnchor.MiddleCenter);
        }

        private void BuildMetricRows()
        {
            const float rowStartY = -220f;
            const float rowHeight = 62f;
            const float rowGap = 4f;

            for (int index = 0; index < metrics.Count; index++)
            {
                HydraulicMetric metric = metrics[index];
                float y = rowStartY - index * (rowHeight + rowGap);
                Color rowColor = index % 2 == 0 ? RowColor : AlternateRowColor;

                GameObject row = CreateImage(metric.Id, detailPanel.transform, rowColor);
                SetTopLeftRect(row.GetComponent<RectTransform>(), new Vector2(20f, y), new Vector2(680f, rowHeight));

                CreateLabel(
                    metric.DisplayName,
                    row.transform,
                    new Vector2(14f, 0f),
                    new Vector2(280f, rowHeight),
                    15,
                    FontStyle.Bold,
                    TextColor,
                    TextAnchor.MiddleLeft);

                Text flow = CreateLabel(
                    FormatValue(metric.FlowRate), row.transform, new Vector2(304f, 0f),
                    new Vector2(118f, rowHeight), 19, FontStyle.Bold, TextColor, TextAnchor.MiddleCenter);
                Text velocity = CreateLabel(
                    FormatValue(metric.Velocity), row.transform, new Vector2(426f, 0f),
                    new Vector2(112f, rowHeight), 19, FontStyle.Bold, TextColor, TextAnchor.MiddleCenter);
                Text pressure = CreateLabel(
                    FormatValue(metric.Pressure), row.transform, new Vector2(542f, 0f),
                    new Vector2(126f, rowHeight), 19, FontStyle.Bold, AccentColor, TextAnchor.MiddleCenter);

                valueLabels[metric.Id] = new[] { flow, velocity, pressure };
            }
        }

        private void RefreshMetric(HydraulicMetric metric)
        {
            if (!valueLabels.TryGetValue(metric.Id, out Text[] labels))
                return;

            labels[0].text = FormatValue(metric.FlowRate);
            labels[1].text = FormatValue(metric.Velocity);
            labels[2].text = FormatValue(metric.Pressure);
        }

        private void CreateTableLabel(
            string label,
            Transform parent,
            float x,
            float width,
            TextAnchor alignment)
        {
            CreateLabel(
                label,
                parent,
                new Vector2(x, 0f),
                new Vector2(width, 48f),
                12,
                FontStyle.Bold,
                MutedTextColor,
                alignment);
        }

        private Button CreateButton(
            string objectName,
            Transform parent,
            Vector2 position,
            Vector2 size,
            string label,
            int fontSize,
            Color backgroundColor)
        {
            GameObject buttonObject = CreateImage(objectName, parent, backgroundColor);
            SetTopLeftRect(buttonObject.GetComponent<RectTransform>(), position, size);

            Button button = buttonObject.AddComponent<Button>();
            ColorBlock colors = button.colors;
            colors.normalColor = Color.white;
            colors.highlightedColor = new Color(1.12f, 1.12f, 1.12f, 1f);
            colors.pressedColor = new Color(0.78f, 0.78f, 0.78f, 1f);
            colors.selectedColor = colors.highlightedColor;
            button.colors = colors;

            CreateLabel(
                label,
                buttonObject.transform,
                Vector2.zero,
                size,
                fontSize,
                FontStyle.Bold,
                Color.white,
                TextAnchor.MiddleCenter);

            return button;
        }

        private GameObject CreateImage(string objectName, Transform parent, Color color)
        {
            GameObject imageObject = new GameObject(objectName, typeof(RectTransform), typeof(Image));
            imageObject.transform.SetParent(parent, false);
            imageObject.layer = LayerMask.NameToLayer("UI");

            Image image = imageObject.GetComponent<Image>();
            image.color = color;
            return imageObject;
        }

        private Text CreateLabel(
            string value,
            Transform parent,
            Vector2 position,
            Vector2 size,
            int fontSize,
            FontStyle fontStyle,
            Color color,
            TextAnchor alignment)
        {
            GameObject labelObject = new GameObject("Text", typeof(RectTransform), typeof(Text));
            labelObject.transform.SetParent(parent, false);
            labelObject.layer = LayerMask.NameToLayer("UI");

            RectTransform rect = labelObject.GetComponent<RectTransform>();
            SetTopLeftRect(rect, position, size);

            Text label = labelObject.GetComponent<Text>();
            label.text = value;
            label.font = uiFont;
            label.fontSize = fontSize;
            label.fontStyle = fontStyle;
            label.color = color;
            label.alignment = alignment;
            label.raycastTarget = false;
            label.horizontalOverflow = HorizontalWrapMode.Wrap;
            label.verticalOverflow = VerticalWrapMode.Truncate;
            return label;
        }

        private static void SetTopLeftRect(RectTransform rect, Vector2 position, Vector2 size)
        {
            rect.anchorMin = new Vector2(0f, 1f);
            rect.anchorMax = new Vector2(0f, 1f);
            rect.pivot = new Vector2(0f, 1f);
            rect.anchoredPosition = position;
            rect.sizeDelta = size;
        }

        private static string FormatValue(float value)
        {
            return value.ToString("0.0");
        }

        private static Font CreateFont()
        {
            Font font = Font.CreateDynamicFontFromOSFont(
                new[] { "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans CJK KR", "Arial" },
                18);

            if (font == null)
                font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");

            return font;
        }

        private static Transform FindDeepChild(Transform root, string objectName)
        {
            if (root == null)
                return null;

            if (root.name == objectName)
                return root;

            foreach (Transform child in root)
            {
                Transform result = FindDeepChild(child, objectName);
                if (result != null)
                    return result;
            }

            return null;
        }

        private static Transform FindLoadedSceneObject(string objectName)
        {
            Transform[] transforms = FindObjectsByType<Transform>(FindObjectsInactive.Include);

            foreach (Transform candidate in transforms)
            {
                Scene scene = candidate.gameObject.scene;
                if (!scene.IsValid() || !scene.isLoaded)
                    continue;

                if (candidate.name.Equals(objectName, StringComparison.Ordinal) ||
                    candidate.name.StartsWith(objectName + " (", StringComparison.Ordinal))
                {
                    return candidate;
                }
            }

            return null;
        }

        private static string GetHierarchyPath(Transform target)
        {
            string path = target.name;
            while (target.parent != null)
            {
                target = target.parent;
                path = target.name + "/" + path;
            }

            return path;
        }

        private static void EnsureEventSystem()
        {
            if (EventSystem.current != null)
                return;

            GameObject eventSystem = new GameObject(
                "EventSystem",
                typeof(EventSystem),
                typeof(StandaloneInputModule));
            eventSystem.layer = LayerMask.NameToLayer("UI");
        }
    }

    internal static class Line11PressDetailBootstrap
    {
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void CreateForCurrentScene()
        {
            if (Line11PressDetailController.Instance != null)
                return;

            GameObject controllerObject = new GameObject("Line11 Press Detail Controller");
            Line11PressDetailController controller =
                controllerObject.AddComponent<Line11PressDetailController>();
            controller.Initialize();
        }
    }
}
