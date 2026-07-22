using System;
using System.Collections.Generic;
using System.Globalization;
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

        private sealed class NormalCycleSample
        {
            public string Phase;
            public string ActiveMode;
            public float TargetPressure;
            public float LoadPressure;
            public float PumpRpm;
            public float OilTemperature;
            public float ReliefSetPressure;
            public float Flow;
            public float SuctionVelocity;
            public float PressureVelocity;
            public float ValvePaVelocity;
            public float ValvePbVelocity;
            public float ValveAtVelocity;
            public float ValveBtVelocity;
            public float ReturnVelocity;
            public float ReliefVelocity;
            public float PumpInPressure;
            public float PumpOutPressure;
            public float ValvePPressure;
            public float ValveAPressure;
            public float ValveBPressure;
            public float ValveTPressure;
            public bool ReliefOpen;
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
        private readonly List<NormalCycleSample> normalCycleSamples =
            new List<NormalCycleSample>();
        private readonly Dictionary<string, Text[]> valueLabels =
            new Dictionary<string, Text[]>(StringComparer.OrdinalIgnoreCase);

        private Transform line11Root;
        private Transform pressFrame;
        private GameObject detailPanel;
        private Camera sceneCamera;
        private Font uiFont;
        private bool initialized;
        private bool detailOpen;
        private bool simulatedLineActive;
        private int displayedLineNumber = 11;
        private float simulatedCycleEpochTime;
        private int lastSimulatedSampleIndex = -1;
        private bool guiStylesReady;
        private bool guiRenderedLogged;
        private Rect sidebarGuiRect;
        private Rect line11ButtonGuiRect;
        private Rect detailGuiRect;
        private Vector2 detailLineScrollPosition;
        private DigitalTwin.View.FactoryTopViewCamera topViewController;
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
        private GUIStyle operatingStatusStyle;
        private GUIStyle detailNavigationButtonStyle;
        private GUIStyle selectedDetailNavigationButtonStyle;
        private GUIStyle detailNavigationLabelStyle;
        private GUIStyle detailNavigationStatusStyle;
        private GUIStyle viewModeButtonStyle;
        private GUIStyle selectedViewModeButtonStyle;
        private Texture2D buttonNormalTexture;
        private Texture2D buttonHoverTexture;
        private Texture2D buttonActiveTexture;
        private Texture2D detailNavigationRowTexture;
        private Texture2D detailNavigationHoverTexture;
        private Texture2D detailNavigationSelectedTexture;
        private Texture2D normalStatusTexture;
        private Texture2D cautionStatusTexture;
        private Texture2D criticalStatusTexture;
        private readonly float[] flowHistory = new float[24];
        private readonly float[] velocityHistory = new float[24];
        private readonly float[] pressureHistory = new float[24];
        private float lastHistoryUpdate;
        private int selectedPipeIndex;
        private int warningAlarmCount = 2;
        private bool solverWarningWasActive;
        private bool liveDataAvailable;
        private int reliefValveOpenCount;
        private bool reliefValveWasOpen;
        private float oilTemperatureC = 44.2f;
        private float pumpRpm = 1450f;
        private float targetPressureBar = 116f;
        private float reliefSetPressureBar = 130f;
        [SerializeField] private string pressSupplyPipeId = "pipe-pump-valve";
        [SerializeField, Min(1f)] private float pressCylinderBoreDiameterMm = 100f;
        [SerializeField, Range(0f, 1f)] private float hydraulicEfficiency = 0.95f;
        [SerializeField, Min(1)] private int pressCylinderCount = 1;
        [SerializeField] private TextAsset normalCycleCsv;
        private readonly DateTime[] alertTimestamps = new DateTime[3];
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
        private string pendingViewSceneName;

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
            simulatedCycleEpochTime = Time.unscaledTime;
            sceneCamera = Camera.main;

            if (sceneCamera == null)
                sceneCamera = FindAnyObjectByType<Camera>();

            CreateDefaultMetrics();
            LoadNormalCycleSamples();
            InitializeRealtimeHistory();
            InitializeAlertTimestamps();
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
        /// IDs: pipe-pump-tank (tank-to-pump suction), pipe-pump-valve
        /// (pump-to-valve discharge), pipe-valve-tank-return,
        /// pipe-valve-tank-relief, motor, valve-pa, valve-bt, valve-pb,
        /// valve-at, relief-valve, cylinder.
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
        /// Updates the Solver warning state and counts only false-to-true transitions.
        /// </summary>
        public void SetSolverWarning(bool isWarning)
        {
            if (isWarning && !solverWarningWasActive)
                warningAlarmCount++;

            solverWarningWasActive = isWarning;
        }

        /// <summary>
        /// Updates operating values received from the live Solver input.
        /// </summary>
        public void SetOperatingMeasurements(float temperatureC, float rpm)
        {
            oilTemperatureC = temperatureC;
            pumpRpm = Mathf.Max(0f, rpm);
        }

        /// <summary>
        /// Sets the accumulated number of relief-valve opening events.
        /// </summary>
        public void SetReliefValveOpenCount(int count)
        {
            reliefValveOpenCount = Mathf.Max(0, count);
        }

        /// <summary>
        /// Updates the current relief-valve state. The count increases only on
        /// a closed-to-open transition, so repeated open samples count once.
        /// </summary>
        public void SetReliefValveOpen(bool isOpen)
        {
            if (isOpen && !reliefValveWasOpen)
                reliefValveOpenCount++;

            reliefValveWasOpen = isOpen;
        }

        /// <summary>
        /// Updates the target operating pressure and configured relief-valve pressure.
        /// </summary>
        public void SetPressureTargets(float targetBar, float reliefSetBar)
        {
            targetPressureBar = Mathf.Max(0f, targetBar);
            reliefSetPressureBar = Mathf.Max(0f, reliefSetBar);
        }

        /// <summary>
        /// Marks whether the dashboard is currently receiving valid live data.
        /// </summary>
        public void SetLiveDataAvailable(bool isAvailable)
        {
            liveDataAvailable = isAvailable;
        }

        /// <summary>
        /// Clears every value populated by the live Solver/MQTT feed.
        /// </summary>
        public void SetAllLiveValuesToZero()
        {
            liveDataAvailable = false;

            foreach (HydraulicMetric metric in metrics)
            {
                metric.FlowRate = 0f;
                metric.Velocity = 0f;
                metric.Pressure = 0f;
                RefreshMetric(metric);
            }

            oilTemperatureC = 0f;
            pumpRpm = 0f;
            targetPressureBar = 0f;
            reliefSetPressureBar = 0f;
            solverWarningWasActive = false;
            reliefValveWasOpen = false;
            warningAlarmCount = 0;
            reliefValveOpenCount = 0;

            Array.Clear(flowHistory, 0, flowHistory.Length);
            Array.Clear(velocityHistory, 0, velocityHistory.Length);
            Array.Clear(pressureHistory, 0, pressureHistory.Length);
        }

        /// <summary>
        /// Updates the hydraulic-cylinder specification used for the live press-force calculation.
        /// </summary>
        public void SetPressCylinderSpecification(float boreDiameterMm, float efficiency, int cylinderCount = 1)
        {
            pressCylinderBoreDiameterMm = Mathf.Max(1f, boreDiameterMm);
            hydraulicEfficiency = Mathf.Clamp01(efficiency);
            pressCylinderCount = Mathf.Max(1, cylinderCount);
        }

        public void OpenDetail()
        {
            bool wasShowingSimulation = simulatedLineActive;
            displayedLineNumber = 11;
            simulatedLineActive = false;
            if (wasShowingSimulation)
                SetAllLiveValuesToZero();

            detailOpen = true;
        }

        public void OpenLineDetail(int lineNumber)
        {
            displayedLineNumber = Mathf.Clamp(lineNumber, 1, 16);
            if (displayedLineNumber == 11)
            {
                OpenDetail();
                return;
            }

            simulatedLineActive = true;
            lastSimulatedSampleIndex = -1;
            SetAllLiveValuesToZero();
            UpdateSimulatedNormalCycle(true);
            detailOpen = true;
        }

        public void CloseDetail()
        {
            detailOpen = false;
            simulatedLineActive = false;
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
            if (detailNavigationRowTexture != null)
                Destroy(detailNavigationRowTexture);
            if (detailNavigationHoverTexture != null)
                Destroy(detailNavigationHoverTexture);
            if (detailNavigationSelectedTexture != null)
                Destroy(detailNavigationSelectedTexture);
            if (normalStatusTexture != null)
                Destroy(normalStatusTexture);
            if (cautionStatusTexture != null)
                Destroy(cautionStatusTexture);
            if (criticalStatusTexture != null)
                Destroy(criticalStatusTexture);

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

            if (!string.IsNullOrEmpty(pendingViewSceneName))
            {
                string sceneName = pendingViewSceneName;
                pendingViewSceneName = null;

                if (!Application.CanStreamedLevelBeLoaded(sceneName))
                {
                    Debug.LogError($"[시점 변경] Build Settings에서 씬을 찾을 수 없습니다: {sceneName}");
                    return;
                }

                Debug.Log($"[시점 변경] {SceneManager.GetActiveScene().name} → {sceneName}");
                SceneManager.LoadSceneAsync(sceneName, LoadSceneMode.Single);
                return;
            }

            if (line11Root == null && Time.frameCount % 30 == 0)
                TryBindLine11();

            UpdateSimulatedNormalCycle(false);
            UpdateRealtimeHistory();
            UpdateAssetPreviewAnimation();

            if (IsOpen && Input.GetKeyDown(KeyCode.Escape))
                CloseDetail();

            if (!Input.GetMouseButtonDown(0))
                return;

            // FactoryTopView owns the left navigation area. Do not raycast through its buttons.
            if (SceneManager.GetActiveScene().name == "FactoryTopView" && Input.mousePosition.x <= 220f)
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
            if (detailOpen)
            {
                sidebarGuiRect = Rect.zero;
                return;
            }

            // The overview scene provides navigation for all 16 lines. Once the Line11 detail
            // view opens, this compact sidebar returns so the existing detail workflow is kept.
            if (!detailOpen && SceneManager.GetActiveScene().name == "FactoryTopView")
            {
                sidebarGuiRect = Rect.zero;
                return;
            }

            const float sidebarWidth = 210f;
            sidebarGuiRect = new Rect(0f, 0f, sidebarWidth, Screen.height);
            line11ButtonGuiRect = Rect.zero;
            DrawDetailLineNavigation(sidebarGuiRect);
        }

        private void DrawViewModeButtons(Rect rect)
        {
            const float gap = 4f;
            float buttonWidth = (rect.width - gap) * 0.5f;
            bool isTopView = SceneManager.GetActiveScene().name == "FactoryTopView";
            GUIStyle topStyle = isTopView ? selectedViewModeButtonStyle : viewModeButtonStyle;
            GUIStyle freeStyle = isTopView ? viewModeButtonStyle : selectedViewModeButtonStyle;

            if (GUI.Button(new Rect(rect.x, rect.y, buttonWidth, rect.height), "탑뷰", topStyle))
                ChangeViewScene("FactoryTopView");
            if (GUI.Button(new Rect(rect.x + buttonWidth + gap, rect.y, buttonWidth, rect.height),
                    "자유 시점", freeStyle))
            {
                ChangeViewScene("FactorySceneSample");
            }
        }

        private void ChangeViewScene(string sceneName)
        {
            if (SceneManager.GetActiveScene().name == sceneName)
            {
                CloseDetail();
                if (sceneName == "FactoryTopView")
                {
                    if (topViewController == null)
                        topViewController = FindAnyObjectByType<DigitalTwin.View.FactoryTopViewCamera>();
                    if (topViewController != null)
                        topViewController.SelectLine(0);
                }
                return;
            }

            pendingViewSceneName = sceneName;
        }

        private void DrawDetailLineNavigation(Rect panel)
        {
            DrawColorRect(panel, new Color32(10, 19, 39, 255));
            if (topViewController == null)
                topViewController = FindAnyObjectByType<DigitalTwin.View.FactoryTopViewCamera>();

            DigitalTwin.View.FactoryTopViewCamera.FactoryStatus factoryStatus =
                topViewController != null
                    ? topViewController.GetFactoryStatus()
                    : DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Normal;

            GUI.Label(new Rect(14f, 10f, panel.width - 28f, 30f),
                "공장 라인 모니터링", smallHeaderStyle);

            Rect factoryCard = new Rect(10f, 45f, panel.width - 20f, 56f);
            DrawColorRect(factoryCard, new Color32(19, 35, 60, 255));
            GUI.DrawTexture(
                new Rect(factoryCard.x + 12f, factoryCard.y + 15f, 26f, 26f),
                GetDetailStatusTexture(factoryStatus),
                ScaleMode.ScaleToFit,
                true);
            GUI.Label(new Rect(factoryCard.x + 48f, factoryCard.y + 4f, 120f, 22f),
                "공장 상태", detailNavigationLabelStyle);
            detailNavigationStatusStyle.normal.textColor = GetDetailStatusColor(factoryStatus);
            GUI.Label(new Rect(factoryCard.x + 48f, factoryCard.y + 25f, 120f, 25f),
                GetDetailStatusText(factoryStatus), detailNavigationStatusStyle);

            DrawDetailStatusLegend(new Rect(10f, 107f, panel.width - 20f, 25f));

            bool showAllLinesButton = detailOpen;
            if (showAllLinesButton)
            {
                Rect allButton = new Rect(10f, 137f, panel.width - 20f, 38f);
                if (GUI.Button(allButton, "전체 라인 보기", detailNavigationButtonStyle))
                {
                    CloseDetail();
                    if (topViewController != null)
                        topViewController.SelectLine(0);
                    return;
                }
            }

            float viewportTop = showAllLinesButton ? 183f : 137f;
            Rect viewport = new Rect(8f, viewportTop, panel.width - 12f,
                Mathf.Max(40f, panel.height - viewportTop - 56f));
            const float rowHeight = 38f;
            const float rowGap = 4f;
            const int lineCount = 16;
            float contentHeight = lineCount * (rowHeight + rowGap) - rowGap;
            Rect content = new Rect(0f, 0f, viewport.width - 18f, contentHeight);
            detailLineScrollPosition = GUI.BeginScrollView(
                viewport,
                detailLineScrollPosition,
                content,
                false,
                false);

            float y = 0f;
            for (int lineNumber = 1; lineNumber <= lineCount; lineNumber++)
            {
                DigitalTwin.View.FactoryTopViewCamera.FactoryStatus status =
                    topViewController != null
                        ? topViewController.GetLineStatus(lineNumber)
                        : DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Normal;
                Rect row = new Rect(0f, y, content.width, rowHeight);
                GUIStyle rowStyle = displayedLineNumber == lineNumber
                    ? selectedDetailNavigationButtonStyle
                    : detailNavigationButtonStyle;

                if (GUI.Button(row, $"라인 {GetLineDisplayName(lineNumber)}", rowStyle))
                {
                    if (topViewController != null)
                        topViewController.SelectLine(lineNumber);
                    else if (lineNumber == 11)
                        OpenDetail();
                    else
                        OpenLineDetail(lineNumber);
                }

                GUI.DrawTexture(
                    new Rect(row.xMax - 67f, row.y + 11f, 16f, 16f),
                    GetDetailStatusTexture(status),
                    ScaleMode.ScaleToFit,
                    true);
                detailNavigationStatusStyle.normal.textColor = GetDetailStatusColor(status);
                GUI.Label(new Rect(row.xMax - 47f, row.y + 7f, 42f, 24f),
                    GetDetailStatusText(status), detailNavigationStatusStyle);
                y += rowHeight + rowGap;
            }

            GUI.EndScrollView();
            GUI.Label(new Rect(10f, panel.yMax - 50f, panel.width - 20f, 18f),
                "시점 변경", detailNavigationLabelStyle);
            DrawViewModeButtons(new Rect(10f, panel.yMax - 32f, panel.width - 20f, 28f));
        }

        private void DrawDetailStatusLegend(Rect rect)
        {
            DigitalTwin.View.FactoryTopViewCamera.FactoryStatus[] statuses =
            {
                DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Normal,
                DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Caution,
                DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Critical
            };
            float itemWidth = rect.width / statuses.Length;
            for (int index = 0; index < statuses.Length; index++)
            {
                Rect item = new Rect(rect.x + itemWidth * index, rect.y, itemWidth, rect.height);
                GUI.DrawTexture(new Rect(item.x + 2f, item.y + 6f, 12f, 12f),
                    GetDetailStatusTexture(statuses[index]), ScaleMode.ScaleToFit, true);
                GUI.Label(new Rect(item.x + 17f, item.y, item.width - 17f, item.height),
                    GetDetailStatusText(statuses[index]), detailNavigationLabelStyle);
            }
        }

        private void DrawDetailGui()
        {
            detailGuiRect = new Rect(0f, 0f, Screen.width, Screen.height);
            DrawColorRect(detailGuiRect, new Color32(20, 23, 26, 255));

            const float navWidth = 210f;
            const float topBarHeight = 46f;
            DrawDetailLineNavigation(new Rect(0f, 0f, navWidth, Screen.height));
            if (!detailOpen)
                return;

            DrawColorRect(new Rect(navWidth, 0f, Screen.width - navWidth, topBarHeight), HeaderColor);

            GUI.Label(
                new Rect(navWidth + 14f, 5f, 380f, 34f),
                simulatedLineActive
                    ? $"<  LINE {GetLineDisplayName(displayedLineNumber)}  정상 사이클"
                    : $"<  LINE {GetLineDisplayName(11)}  유압 프레스",
                smallHeaderStyle);
            GUI.Label(
                new Rect(Screen.width - 210f, 5f, 140f, 34f),
                "실시간 모니터링",
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
            float statusHeight = Mathf.Clamp(column.height * 0.12f, 74f, 94f);
            float cycleHeight = column.height * 0.58f;
            Rect statusPanel = new Rect(column.x, column.y, column.width, statusHeight);
            Rect cyclePanel = new Rect(column.x, statusPanel.yMax + gap, column.width, cycleHeight);
            Rect conditionPanel = new Rect(
                column.x,
                cyclePanel.yMax + gap,
                column.width,
                column.yMax - cyclePanel.yMax - gap);

            DrawCycleStatusPanel(statusPanel);
            DrawRamPositionPanel(cyclePanel);
            DrawConditionPanel(conditionPanel);
        }

        private void DrawCycleStatusPanel(Rect rect)
        {
            DrawColorRect(rect, PanelColor);
            GUI.Label(
                new Rect(rect.x + 12f, rect.y + 6f, rect.width - 24f, 26f),
                "프레스 운전 상태",
                smallHeaderStyle);

            Rect statusChip = new Rect(rect.x + 12f, rect.y + 38f, rect.width - 24f, 26f);
            DrawColorRect(statusChip, SuccessColor);
            GUI.Label(statusChip, "운전 중", operatingStatusStyle);
        }

        private void DrawRamPositionPanel(Rect rect)
        {
            DrawColorRect(rect, PanelColor);
            GUI.Label(
                new Rect(rect.x + 12f, rect.y + 6f, rect.width - 24f, 26f),
                "프레스 압력 / 가압력",
                smallHeaderStyle);

            Rect previewRect = new Rect(
                rect.x + 10f,
                rect.y + 36f,
                rect.width - 20f,
                Mathf.Max(62f, rect.height - 102f));
            DrawColorRect(previewRect, new Color32(31, 35, 39, 255));
            if (assetPreviewTexture != null)
                GUI.DrawTexture(previewRect, assetPreviewTexture, ScaleMode.ScaleAndCrop, false);
            else
                GUI.Label(previewRect, "프레스 프레임 설비", tableHeaderStyle);

            float currentPressure = GetPressSupplyPressureBar();
            float pressForce = CalculatePressForceTon(currentPressure);
            float cardGap = 5f;
            float cardY = previewRect.yMax + 5f;
            float cardHeight = Mathf.Max(34f, rect.yMax - 10f - cardY);
            float cardWidth = (rect.width - 20f - cardGap) / 2f;
            float cardX = rect.x + 10f;

            DrawCycleValueCard(
                new Rect(cardX, cardY, cardWidth, cardHeight),
                "공급 파이프 압력",
                $"{currentPressure:0.0} bar",
                TextColor);
            cardX += cardWidth + cardGap;
            DrawCycleValueCard(
                new Rect(cardX, cardY, cardWidth, cardHeight),
                "가압력",
                $"{pressForce:0.0} ton",
                new Color32(224, 172, 69, 255));
        }

        private float GetPressSupplyPressureBar()
        {
            HydraulicMetric supplyPipe = metrics.Find(
                item => string.Equals(item.Id, pressSupplyPipeId, StringComparison.OrdinalIgnoreCase));

            return supplyPipe != null ? Mathf.Max(0f, supplyPipe.Pressure) : 0f;
        }

        private float CalculatePressForceTon(float pressureBar)
        {
            float boreDiameterCm = pressCylinderBoreDiameterMm * 0.1f;
            float pistonAreaCm2 = Mathf.PI * boreDiameterCm * boreDiameterCm * 0.25f;

            // 1 bar = 10 N/cm2, and 1 metric ton-force = 9806.65 N.
            return pressureBar * pistonAreaCm2 * hydraulicEfficiency * pressCylinderCount / 980.665f;
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
                "상태 요약",
                smallHeaderStyle);

            float gap = 6f;
            float cardWidth = (rect.width - 30f) * 0.5f;
            float cardHeight = Mathf.Max(38f, (rect.height - 46f - gap) * 0.5f);
            float firstX = rect.x + 12f;
            float secondX = firstX + cardWidth + gap;
            float firstY = rect.y + 34f;
            float secondY = firstY + cardHeight + gap;

            DrawConditionCard(new Rect(firstX, firstY, cardWidth, cardHeight),
                "오일 온도", $"{oilTemperatureC:0.0} C", SuccessColor);
            DrawConditionCard(new Rect(secondX, firstY, cardWidth, cardHeight),
                "펌프 회전수", $"{pumpRpm:0} RPM", AccentColor);
            DrawConditionCard(new Rect(firstX, secondY, cardWidth, cardHeight),
                "릴리프 밸브 개도 횟수", $"{reliefValveOpenCount}회", SuccessColor);
            DrawConditionCard(new Rect(secondX, secondY, cardWidth, cardHeight),
                "경고", $"{warningAlarmCount}건", new Color32(216, 110, 76, 255));
        }

        private void DrawConditionCard(Rect rect, string label, string value, Color accent)
        {
            DrawColorRect(rect, HeaderColor);
            GUI.Label(new Rect(rect.x + 7f, rect.y + 2f, rect.width - 12f, 18f), label, footnoteStyle);
            GUI.Label(new Rect(rect.x + 7f, rect.y + 17f, rect.width - 12f, rect.height - 19f),
                value, valueStyle);
            DrawColorRect(new Rect(rect.x, rect.yMax - 3f, rect.width, 3f), accent);
        }

        private void GetPressCycleState(
            out float strokeNormalized,
            out string stage,
            out float cyclePhase)
        {
            if (simulatedLineActive && normalCycleSamples.Count > 0)
            {
                float elapsed = GetSimulatedCycleElapsed();
                int sampleIndex = Mathf.FloorToInt(elapsed) % normalCycleSamples.Count;
                NormalCycleSample sample = normalCycleSamples[sampleIndex];
                cyclePhase = sampleIndex / (float)normalCycleSamples.Count;

                if (sample.ActiveMode == "downstroke")
                {
                    stage = sample.Phase;
                    int downstrokeCount = CountCycleModeSamples("downstroke");
                    strokeNormalized = Mathf.Clamp01((sampleIndex + 1f) / Mathf.Max(1, downstrokeCount));
                }
                else if (sample.ActiveMode == "upstroke")
                {
                    stage = sample.Phase;
                    int firstUpstroke = FindFirstCycleModeSample("upstroke");
                    int upstrokeCount = CountCycleModeSamples("upstroke");
                    int upstrokeIndex = Mathf.Max(0, sampleIndex - firstUpstroke);
                    strokeNormalized = 1f - Mathf.Clamp01(
                        (upstrokeIndex + 1f) / Mathf.Max(1, upstrokeCount));
                }
                else
                {
                    stage = sample.Phase;
                    strokeNormalized = 1f;
                }

                return;
            }

            cyclePhase = Mathf.Repeat(Time.unscaledTime / 8f, 1f);

            if (cyclePhase < 0.15f)
            {
                stage = "준비 중";
                strokeNormalized = 0f;
            }
            else if (cyclePhase < 0.55f)
            {
                stage = "가압 중";
                strokeNormalized = Mathf.InverseLerp(0.15f, 0.55f, cyclePhase);
            }
            else if (cyclePhase < 0.70f)
            {
                stage = "유지 중";
                strokeNormalized = 1f;
            }
            else
            {
                stage = "복귀 중";
                strokeNormalized = 1f - Mathf.InverseLerp(0.70f, 1f, cyclePhase);
            }
        }

        private void UpdateSimulatedNormalCycle(bool forceUpdate)
        {
            if (!simulatedLineActive || normalCycleSamples.Count == 0)
                return;

            float elapsed = GetSimulatedCycleElapsed();
            int sampleIndex = Mathf.FloorToInt(elapsed) % normalCycleSamples.Count;
            if (!forceUpdate && sampleIndex == lastSimulatedSampleIndex)
                return;

            lastSimulatedSampleIndex = sampleIndex;
            NormalCycleSample sample = normalCycleSamples[sampleIndex];
            float cylinderVelocity = Mathf.Max(
                Mathf.Max(sample.ValvePaVelocity, sample.ValvePbVelocity),
                Mathf.Max(sample.ValveAtVelocity, sample.ValveBtVelocity));

            SetMetric("pipe-pump-tank", sample.Flow, sample.SuctionVelocity,
                sample.PumpInPressure);
            SetMetric("pipe-pump-valve", sample.Flow, sample.PressureVelocity,
                sample.ValvePPressure);
            SetMetric("pipe-valve-tank-return", sample.Flow, sample.ReturnVelocity,
                sample.ValveTPressure);
            SetMetric("pipe-valve-tank-relief", sample.ReliefOpen ? sample.Flow : 0f,
                sample.ReliefVelocity, sample.ValvePPressure);
            SetMetric("motor", sample.Flow, sample.PressureVelocity,
                sample.PumpOutPressure);
            SetMetric("valve-pa", ActiveSimulatedPathFlow(sample.Flow, sample.ValvePaVelocity),
                sample.ValvePaVelocity, sample.ValvePPressure);
            SetMetric("valve-bt", ActiveSimulatedPathFlow(sample.Flow, sample.ValveBtVelocity),
                sample.ValveBtVelocity, sample.ValveBPressure);
            SetMetric("valve-pb", ActiveSimulatedPathFlow(sample.Flow, sample.ValvePbVelocity),
                sample.ValvePbVelocity, sample.ValvePPressure);
            SetMetric("valve-at", ActiveSimulatedPathFlow(sample.Flow, sample.ValveAtVelocity),
                sample.ValveAtVelocity, sample.ValveAPressure);
            SetMetric("relief-valve", sample.ReliefOpen ? sample.Flow : 0f,
                sample.ReliefVelocity, sample.ValvePPressure);
            SetMetric("cylinder", cylinderVelocity > 0f ? sample.Flow : 0f,
                cylinderVelocity, sample.LoadPressure);

            SetOperatingMeasurements(sample.OilTemperature, sample.PumpRpm);
            SetPressureTargets(sample.TargetPressure, sample.ReliefSetPressure);
            SetSolverWarning(false);
            SetReliefValveOpen(sample.ReliefOpen);
            SetLiveDataAvailable(true);
            AppendCurrentMetricToHistory();
        }

        private static float ActiveSimulatedPathFlow(float flow, float velocity)
        {
            return velocity > 0.0001f ? flow : 0f;
        }

        private float GetSimulatedCycleElapsed()
        {
            return Mathf.Max(0f, Time.unscaledTime - simulatedCycleEpochTime);
        }

        private int CountCycleModeSamples(string activeMode)
        {
            int count = 0;
            foreach (NormalCycleSample sample in normalCycleSamples)
            {
                if (string.Equals(sample.ActiveMode, activeMode, StringComparison.OrdinalIgnoreCase))
                    count++;
            }

            return count;
        }

        private int FindFirstCycleModeSample(string activeMode)
        {
            for (int index = 0; index < normalCycleSamples.Count; index++)
            {
                if (string.Equals(
                        normalCycleSamples[index].ActiveMode,
                        activeMode,
                        StringComparison.OrdinalIgnoreCase))
                {
                    return index;
                }
            }

            return 0;
        }

        private void DrawSchematicAndMetrics(Rect panel)
        {
            DrawColorRect(panel, PanelColor);
            GUI.Label(
                new Rect(panel.x + 12f, panel.y + 6f, panel.width - 24f, 28f),
                "압력 설정 / 구성요소 계측",
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
                "압력 설정값", footnoteStyle);

            const float gap = 8f;
            float cardWidth = (rect.width - 20f - gap) * 0.5f;
            Rect targetCard = new Rect(rect.x + 10f, rect.y + 28f, cardWidth, 54f);
            Rect reliefCard = new Rect(targetCard.xMax + gap, targetCard.y, cardWidth, targetCard.height);
            DrawPressureSettingCard(targetCard, "목표 압력", targetPressureBar, AccentColor);
            DrawPressureSettingCard(reliefCard, "릴리프 설정 압력", reliefSetPressureBar,
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
                "구성요소", tableHeaderStyle);
            GUI.Label(new Rect(rect.x + componentWidth, rect.y, valueWidth, headerHeight),
                "유량\nL/min", tableHeaderStyle);
            GUI.Label(new Rect(rect.x + componentWidth + valueWidth, rect.y, valueWidth, headerHeight),
                "유속\nm/s", tableHeaderStyle);
            GUI.Label(new Rect(rect.x + componentWidth + valueWidth * 2f, rect.y, valueWidth, headerHeight),
                "압력\nbar", tableHeaderStyle);

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
                "설비 계측", smallHeaderStyle);

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
                "유효 가동률", tableHeaderStyle);
        }

        private void DrawProfilePanel(Rect rect)
        {
            DrawColorRect(rect, PanelColor);
            GUI.Label(new Rect(rect.x + 12f, rect.y + 6f, rect.width - 24f, 26f),
                "압력 분포", smallHeaderStyle);

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
                "운영 안전 정보", smallHeaderStyle);
            GUI.Label(new Rect(rect.x + 12f, rect.y + 38f, rect.width - 24f, 42f),
                "위험 설명\n높은 유압으로 표면 과열 또는 유체 누출이 발생할 수 있습니다.", bodyStyle);
            GUI.Label(new Rect(rect.x + 12f, rect.y + 90f, rect.width - 24f, 54f),
                "조치 사항\n보안경을 착용하고 정비 전 압력을 차단한 후 씰을 점검하세요.", bodyStyle);

            float indicatorY = rect.yMax - 34f;
            DrawColorRect(new Rect(rect.x + 14f, indicatorY + 4f, 12f, 12f), SuccessColor);
            GUI.Label(new Rect(rect.x + 34f, indicatorY, rect.width - 48f, 22f),
                "발생 가능성     낮음", tableHeaderStyle);
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
                "실시간 보기", smallHeaderStyle);

            DrawPipeSelector(realtime);

            float chartGap = 6f;
            float chartStartY = realtime.y + 68f;
            float chartHeight = (realtime.yMax - chartStartY - chartGap * 2f - 8f) / 3f;
            DrawChart(new Rect(realtime.x + 10f, chartStartY, realtime.width - 20f, chartHeight),
                "유량", flowHistory, 0f, 55f, "L/min", AccentColor);
            DrawChart(new Rect(realtime.x + 10f, chartStartY + chartHeight + chartGap,
                    realtime.width - 20f, chartHeight),
                "유속", velocityHistory, 0f, 4f, "m/s", new Color32(35, 181, 224, 255));
            DrawChart(new Rect(realtime.x + 10f, chartStartY + (chartHeight + chartGap) * 2f,
                    realtime.width - 20f, chartHeight),
                "압력", pressureHistory, 0f, 160f, "bar", new Color32(85, 141, 212, 255));

            DrawRecommendations(recommendations);
        }

        private void DrawPipeSelector(Rect realtimePanel)
        {
            string[] buttonLabels =
            {
                "흡입  탱크→펌프",
                "토출  펌프→밸브P",
                "복귀  밸브T→탱크",
                "릴리프  밸브P→탱크"
            };
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
                "권장사항 / 알람", smallHeaderStyle);

            InitializeAlertTimestamps();

            string[] alerts =
            {
                $"{alertTimestamps[0]:yyyy-MM-dd HH:mm:ss}  안전 경고: 유압 점검이 필요합니다",
                $"{alertTimestamps[1]:yyyy-MM-dd HH:mm:ss}  밸브-탱크 리턴 유량 안정성을 확인하세요",
                $"{alertTimestamps[2]:yyyy-MM-dd HH:mm:ss}  실린더 씰 온도가 정상 범위입니다"
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

        private void InitializeAlertTimestamps()
        {
            if (alertTimestamps[0] != DateTime.MinValue)
                return;

            DateTime now = DateTime.Now;
            alertTimestamps[0] = now.AddMinutes(-18d);
            alertTimestamps[1] = now.AddMinutes(-9d);
            alertTimestamps[2] = now.AddMinutes(-2d);
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
            if (!liveDataAvailable)
                return;

            if (simulatedLineActive)
                return;

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

        private void AppendCurrentMetricToHistory()
        {
            HydraulicMetric selectedPipe = GetSelectedPipeMetric();
            if (selectedPipe == null)
                return;

            ShiftHistory(flowHistory);
            ShiftHistory(velocityHistory);
            ShiftHistory(pressureHistory);
            flowHistory[flowHistory.Length - 1] = selectedPipe.FlowRate;
            velocityHistory[velocityHistory.Length - 1] = selectedPipe.Velocity;
            pressureHistory[pressureHistory.Length - 1] = selectedPipe.Pressure;
            lastHistoryUpdate = Time.unscaledTime;
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
                closeButtonStyle != null && pipeButtonStyle != null && operatingStatusStyle != null &&
                detailNavigationButtonStyle != null && normalStatusTexture != null &&
                viewModeButtonStyle != null && selectedViewModeButtonStyle != null)
                return;

            guiStylesReady = true;
            buttonNormalTexture = CreateColorTexture(AccentColor);
            buttonHoverTexture = CreateColorTexture(new Color32(39, 178, 217, 255));
            buttonActiveTexture = CreateColorTexture(new Color32(18, 124, 160, 255));
            detailNavigationRowTexture = CreateColorTexture(new Color32(19, 35, 60, 255));
            detailNavigationHoverTexture = CreateColorTexture(new Color32(24, 67, 96, 255));
            detailNavigationSelectedTexture = CreateColorTexture(new Color32(19, 82, 113, 255));
            normalStatusTexture = CreateCircleTexture(
                GetDetailStatusColor(DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Normal));
            cautionStatusTexture = CreateCircleTexture(
                GetDetailStatusColor(DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Caution));
            criticalStatusTexture = CreateCircleTexture(
                GetDetailStatusColor(DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Critical));

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
            operatingStatusStyle = CreateGuiLabelStyle(
                14,
                FontStyle.Bold,
                Color.black,
                TextAnchor.MiddleCenter);

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

            detailNavigationButtonStyle = new GUIStyle(GUI.skin.button)
            {
                font = uiFont,
                fontSize = 14,
                fontStyle = FontStyle.Bold,
                alignment = TextAnchor.MiddleLeft,
                padding = new RectOffset(12, 70, 0, 0)
            };
            detailNavigationButtonStyle.normal.background = detailNavigationRowTexture;
            detailNavigationButtonStyle.hover.background = detailNavigationHoverTexture;
            detailNavigationButtonStyle.active.background = detailNavigationSelectedTexture;
            detailNavigationButtonStyle.normal.textColor = TextColor;
            detailNavigationButtonStyle.hover.textColor = Color.white;
            detailNavigationButtonStyle.active.textColor = Color.white;

            selectedDetailNavigationButtonStyle = new GUIStyle(detailNavigationButtonStyle);
            selectedDetailNavigationButtonStyle.normal.background = detailNavigationSelectedTexture;
            detailNavigationLabelStyle = CreateGuiLabelStyle(
                11, FontStyle.Normal, MutedTextColor, TextAnchor.MiddleLeft);
            detailNavigationStatusStyle = CreateGuiLabelStyle(
                12, FontStyle.Bold, SuccessColor, TextAnchor.MiddleLeft);

            viewModeButtonStyle = new GUIStyle(GUI.skin.button)
            {
                font = uiFont,
                fontSize = 12,
                fontStyle = FontStyle.Bold,
                alignment = TextAnchor.MiddleCenter,
                padding = new RectOffset(2, 2, 0, 0)
            };
            viewModeButtonStyle.normal.background = detailNavigationRowTexture;
            viewModeButtonStyle.hover.background = detailNavigationHoverTexture;
            viewModeButtonStyle.active.background = detailNavigationSelectedTexture;
            viewModeButtonStyle.normal.textColor = TextColor;
            viewModeButtonStyle.hover.textColor = Color.white;
            viewModeButtonStyle.active.textColor = Color.white;

            selectedViewModeButtonStyle = new GUIStyle(viewModeButtonStyle);
            selectedViewModeButtonStyle.normal.background = detailNavigationSelectedTexture;
            selectedViewModeButtonStyle.normal.textColor = Color.white;

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

        private static Texture2D CreateCircleTexture(Color color)
        {
            const int size = 32;
            Texture2D texture = new Texture2D(size, size, TextureFormat.RGBA32, false)
            {
                hideFlags = HideFlags.HideAndDontSave,
                filterMode = FilterMode.Bilinear,
                wrapMode = TextureWrapMode.Clamp
            };
            Vector2 center = new Vector2((size - 1) * 0.5f, (size - 1) * 0.5f);
            float radius = size * 0.40f;
            for (int y = 0; y < size; y++)
            {
                for (int x = 0; x < size; x++)
                {
                    float distance = Vector2.Distance(new Vector2(x, y), center);
                    float alpha = Mathf.Clamp01(radius - distance + 1f);
                    texture.SetPixel(x, y,
                        new Color(color.r, color.g, color.b, color.a * alpha));
                }
            }

            texture.Apply();
            return texture;
        }

        private Texture2D GetDetailStatusTexture(
            DigitalTwin.View.FactoryTopViewCamera.FactoryStatus status)
        {
            switch (status)
            {
                case DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Critical:
                    return criticalStatusTexture;
                case DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Caution:
                    return cautionStatusTexture;
                default:
                    return normalStatusTexture;
            }
        }

        private static Color GetDetailStatusColor(
            DigitalTwin.View.FactoryTopViewCamera.FactoryStatus status)
        {
            switch (status)
            {
                case DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Critical:
                    return new Color32(235, 77, 77, 255);
                case DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Caution:
                    return new Color32(238, 159, 56, 255);
                default:
                    return new Color32(64, 210, 137, 255);
            }
        }

        private static string GetDetailStatusText(
            DigitalTwin.View.FactoryTopViewCamera.FactoryStatus status)
        {
            switch (status)
            {
                case DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Critical:
                    return "치명";
                case DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Caution:
                    return "주의";
                default:
                    return "정상";
            }
        }

        private string GetLineDisplayName(int lineNumber)
        {
            if (topViewController == null)
                topViewController = FindAnyObjectByType<DigitalTwin.View.FactoryTopViewCamera>();

            if (topViewController != null)
                return topViewController.GetLineDisplayName(lineNumber);

            int zeroBased = Mathf.Clamp(lineNumber - 1, 0, 15);
            char column = (char)('A' + zeroBased % 4);
            int row = zeroBased / 4 + 1;
            return $"{column}{row}";
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

        private void LoadNormalCycleSamples()
        {
            normalCycleSamples.Clear();
            TextAsset source = normalCycleCsv != null
                ? normalCycleCsv
                : Resources.Load<TextAsset>("LineNormalCycle");
            if (source == null)
            {
                Debug.LogWarning("[Line UI] Resources/LineNormalCycle.csv was not found.");
                return;
            }

            string[] lines = source.text.Split(new[] { '\r', '\n' },
                StringSplitOptions.RemoveEmptyEntries);
            for (int lineIndex = 1; lineIndex < lines.Length; lineIndex++)
            {
                string[] values = lines[lineIndex].Split(',');
                if (values.Length < 24)
                {
                    Debug.LogWarning($"[Line UI] Invalid normal-cycle CSV row: {lineIndex + 1}");
                    continue;
                }

                normalCycleSamples.Add(new NormalCycleSample
                {
                    Phase = CleanCsvValue(values[1]),
                    ActiveMode = CleanCsvValue(values[2]),
                    TargetPressure = ParseCsvFloat(values[3]),
                    LoadPressure = ParseCsvFloat(values[4]),
                    PumpRpm = ParseCsvFloat(values[5]),
                    OilTemperature = ParseCsvFloat(values[6]),
                    ReliefSetPressure = ParseCsvFloat(values[7]),
                    Flow = ParseCsvFloat(values[8]),
                    SuctionVelocity = ParseCsvFloat(values[9]),
                    PressureVelocity = ParseCsvFloat(values[10]),
                    ValvePaVelocity = ParseCsvFloat(values[11]),
                    ValvePbVelocity = ParseCsvFloat(values[12]),
                    ValveAtVelocity = ParseCsvFloat(values[13]),
                    ValveBtVelocity = ParseCsvFloat(values[14]),
                    ReturnVelocity = ParseCsvFloat(values[15]),
                    ReliefVelocity = ParseCsvFloat(values[16]),
                    PumpInPressure = ParseCsvFloat(values[17]),
                    PumpOutPressure = ParseCsvFloat(values[18]),
                    ValvePPressure = ParseCsvFloat(values[19]),
                    ValveAPressure = ParseCsvFloat(values[20]),
                    ValveBPressure = ParseCsvFloat(values[21]),
                    ValveTPressure = ParseCsvFloat(values[22]),
                    ReliefOpen = ParseCsvBool(values[23])
                });
            }

            Debug.Log($"[Line UI] Loaded {normalCycleSamples.Count} one-second normal-cycle CSV samples.");
        }

        private static float ParseCsvFloat(string value)
        {
            return float.TryParse(
                CleanCsvValue(value),
                NumberStyles.Float,
                CultureInfo.InvariantCulture,
                out float result)
                ? result
                : 0f;
        }

        private static bool ParseCsvBool(string value)
        {
            string cleaned = CleanCsvValue(value);
            return cleaned.Equals("true", StringComparison.OrdinalIgnoreCase) ||
                cleaned.Equals("yes", StringComparison.OrdinalIgnoreCase) ||
                cleaned.Equals("open", StringComparison.OrdinalIgnoreCase) ||
                cleaned == "1";
        }

        private static string CleanCsvValue(string value)
        {
            return value.Trim().Trim('"');
        }

        private void CreateDefaultMetrics()
        {
            // Temporary sample values. Replace these through SetMetric when live data is connected.
            metrics.Add(new HydraulicMetric(
                "pipe-pump-tank", "흡입 배관  탱크→펌프", 35.2f, 2.30f, 12.0f));
            metrics.Add(new HydraulicMetric(
                "pipe-pump-valve", "토출 배관  펌프→밸브P", 42.0f, 2.80f, 125.0f));
            metrics.Add(new HydraulicMetric(
                "pipe-valve-tank-return", "복귀 배관  밸브T→탱크", 34.8f, 2.20f, 10.5f));
            metrics.Add(new HydraulicMetric(
                "pipe-valve-tank-relief", "릴리프 배관  밸브P→탱크", 4.1f, 0.45f, 126.0f));
            metrics.Add(new HydraulicMetric(
                "motor", "펌프 구동 모터", 40.0f, 1.85f, 122.0f));
            metrics.Add(new HydraulicMetric(
                "valve-pa", "방향밸브 P→A  하강 공급", 39.1f, 2.60f, 120.0f));
            metrics.Add(new HydraulicMetric(
                "valve-bt", "방향밸브 B→T  하강 복귀", 39.1f, 2.40f, 10.5f));
            metrics.Add(new HydraulicMetric(
                "valve-pb", "방향밸브 P→B  상승 공급", 0f, 0f, 120.0f));
            metrics.Add(new HydraulicMetric(
                "valve-at", "방향밸브 A→T  상승 복귀", 0f, 0f, 10.5f));
            metrics.Add(new HydraulicMetric(
                "relief-valve", "릴리프 밸브 P→T  과압 배출", 0f, 0f, 126.0f));
            metrics.Add(new HydraulicMetric(
                "cylinder", "프레스 실린더  캡/로드", 37.8f, 0.42f, 116.0f));
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
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        private static void RegisterSceneLoadCallback()
        {
            SceneManager.sceneLoaded -= HandleSceneLoaded;
            SceneManager.sceneLoaded += HandleSceneLoaded;
        }

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

        private static void HandleSceneLoaded(Scene scene, LoadSceneMode mode)
        {
            CreateForCurrentScene();
        }
    }
}
