using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.SceneManagement;

namespace DigitalTwin.View
{
    /// <summary>
    /// Frames every renderer below the Line root in a fixed, straight-down view.
    /// This controller is created automatically only in the FactoryTopView scene.
    /// </summary>
    [DefaultExecutionOrder(10000)]
    public sealed class FactoryTopViewCamera : MonoBehaviour
    {
        public enum FactoryStatus
        {
            Normal = 0,
            Caution = 1,
            Critical = 2
        }

        private const string TopViewSceneName = "FactoryTopView";
        private const string LineRootName = "Line";
        private const int ActivityLayer = 28;
        private const int StructureLayer = 29;
        private const int TopViewLayer = 30;
        private const int PreviewLayer = 31;
        private const float ViewMargin = 1.18f;
        private const float CeilingOpacity = 0.12f;
        private const float FactoryLightMultiplier = 0.50f;
        private const float WallLightBand = 3.5f;
        private const float ViewPitch = 58f;
        private const float ViewYaw = 0f;
        private const float MaximumVisibleWorldHeight = 6f;
        private const float FloatingBeamMinimumHeight = 2.5f;
        private const float FloatingBeamMinimumLength = 6f;
        private const float FloatingBeamAspectRatio = 8f;
        private const float NavigationWidth = 210f;
        private const int LineCount = 16;
        private const float StatusMarkerSize = 38f;

        private Camera topViewCamera;
        private Vector3 cameraPosition;
        private Quaternion cameraRotation;
        private float orthographicSize;
        private float farClipPlane;
        private bool viewReady;
        private bool factoryLightingAdjusted;
        private Bounds factoryViewBounds;
        private bool hasFactoryViewBounds;
        private int selectedLineNumber;
        private readonly FactoryStatus[] lineStatuses = new FactoryStatus[LineCount];
        private readonly Vector3[] lineMarkerWorldPositions = new Vector3[LineCount];
        private readonly bool[] hasLineMarkerPosition = new bool[LineCount];
        private readonly string[] lineDisplayNames = new string[LineCount];
        private Vector2 lineScrollPosition;
        private GUIStyle navigationTitleStyle;
        private GUIStyle navigationButtonStyle;
        private GUIStyle selectedNavigationButtonStyle;
        private GUIStyle navigationLabelStyle;
        private GUIStyle statusLabelStyle;
        private GUIStyle legendStyle;
        private GUIStyle mapMarkerLabelStyle;
        private GUIStyle mapMarkerButtonStyle;
        private GUIStyle viewModeButtonStyle;
        private GUIStyle selectedViewModeButtonStyle;
        private Texture2D navigationPanelTexture;
        private Texture2D navigationRowTexture;
        private Texture2D navigationHoverTexture;
        private Texture2D navigationSelectedTexture;
        private Texture2D accentTexture;
        private Texture2D normalStatusTexture;
        private Texture2D cautionStatusTexture;
        private Texture2D criticalStatusTexture;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        private static void RegisterSceneLoadCallback()
        {
            SceneManager.sceneLoaded -= HandleSceneLoaded;
            SceneManager.sceneLoaded += HandleSceneLoaded;
        }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void CreateForTopViewScene()
        {
            EnsureControllerForScene(SceneManager.GetActiveScene());
        }

        private static void HandleSceneLoaded(Scene scene, LoadSceneMode mode)
        {
            EnsureControllerForScene(scene);
        }

        private static void EnsureControllerForScene(Scene scene)
        {
            if (!scene.IsValid() || scene.name != TopViewSceneName)
                return;

            if (FindAnyObjectByType<FactoryTopViewCamera>() != null)
                return;

            GameObject controllerObject = new GameObject("Factory Top View Controller");
            SceneManager.MoveGameObjectToScene(controllerObject, scene);
            controllerObject.AddComponent<FactoryTopViewCamera>();
        }

        private IEnumerator Start()
        {
            // Wait one frame so prefab renderers and the player camera finish initializing.
            yield return null;
            InitializeLineStatuses();
            RefreshView();
        }

        /// <summary>
        /// Updates one line status. The factory status automatically follows the most severe line.
        /// </summary>
        public void SetLineStatus(int lineNumber, FactoryStatus status)
        {
            if (lineNumber < 1 || lineNumber > LineCount)
            {
                Debug.LogWarning($"[탑뷰] 잘못된 라인 번호입니다: {lineNumber}");
                return;
            }

            lineStatuses[lineNumber - 1] = status;
        }

        /// <summary>
        /// Recalculates the top view from the current Line/01~16 renderer bounds.
        /// </summary>
        public void RefreshView()
        {
            Scene activeScene = SceneManager.GetActiveScene();
            HideRenderersAboveWorldHeight(activeScene, MaximumVisibleWorldHeight);
            Transform lineRoot = FindLineRoot(activeScene);
            if (lineRoot == null)
            {
                Debug.LogWarning("[탑뷰] Line 루트를 찾지 못했습니다.");
                return;
            }

            Renderer[] lineRenderers = lineRoot.GetComponentsInChildren<Renderer>(true);
            if (!TryCalculateBounds(lineRenderers, out Bounds lineBounds))
            {
                Debug.LogWarning("[탑뷰] 라인 렌더러 범위를 계산하지 못했습니다.");
                return;
            }

            AssignLayer(lineRenderers, TopViewLayer);
            CacheLineMarkerPositions(lineRoot);

            Renderer[] structureRenderers = CollectStructureRenderers(activeScene, out Transform ceilingRoot);
            AssignLayer(structureRenderers, StructureLayer);
            Transform ceilingFixtureRoot = FindSceneTransform(activeScene, "Light");
            int removedWallWindowCount = RemoveWallWindowVisuals(
                activeScene,
                FindSceneTransform(activeScene, "Building"));
            int removedCeilingStructureCount = PrepareCeilingForTopView(
                structureRenderers,
                ceilingRoot,
                ceilingFixtureRoot);
            removedCeilingStructureCount += RemoveCeilingLightVisuals(activeScene);
            removedCeilingStructureCount += RemoveLongHorizontalStructureLines(
                activeScene,
                lineBounds);

            // Keep workers, carts, props, and controller visuals in the top-view camera.
            // Their Animator and behaviour components are left untouched, so all motion continues.
            Renderer[] activityRenderers = CollectActivityRenderers(activeScene);
            AssignLayer(activityRenderers, ActivityLayer);

            Bounds viewBounds = lineBounds;
            Bounds wallBounds = lineBounds;
            if (TryCalculateBounds(structureRenderers, out Bounds structureBounds))
            {
                viewBounds.Encapsulate(structureBounds);
                wallBounds = structureBounds;
            }
            if (TryCalculateBounds(activityRenderers, out Bounds activityBounds))
                viewBounds.Encapsulate(activityBounds);

            ConfigureFactoryLighting(activeScene, wallBounds);

            topViewCamera = FindSceneCamera(activeScene);
            if (topViewCamera == null)
            {
                GameObject cameraObject = new GameObject("Factory Top View Camera", typeof(Camera));
                SceneManager.MoveGameObjectToScene(cameraObject, activeScene);
                topViewCamera = cameraObject.GetComponent<Camera>();
                cameraObject.tag = "MainCamera";
            }

            factoryViewBounds = viewBounds;
            hasFactoryViewBounds = true;
            FrameBounds(viewBounds, ViewMargin);
            Debug.Log(
                $"[탑뷰] Line/01~16과 공장 구조를 표시합니다. 중심={viewBounds.center}, " +
                $"범위={viewBounds.size}, 카메라 크기={orthographicSize:0.0}, " +
                $"천장 부착 구조물 제거={removedCeilingStructureCount}개");
        }

        private void LateUpdate()
        {
            if (viewReady)
                ApplyTopView();
        }

        private void OnGUI()
        {
            if (!viewReady)
                return;

            DigitalTwin.Line11.Line11PressDetailController line11Detail =
                DigitalTwin.Line11.Line11PressDetailController.Instance;
            if (line11Detail != null && line11Detail.IsOpen)
                return;

            EnsureNavigationStyles();
            DrawFactoryStatusMarkers();
            DrawLineNavigation();
        }

        private void OnDestroy()
        {
            DestroyTexture(navigationPanelTexture);
            DestroyTexture(navigationRowTexture);
            DestroyTexture(navigationHoverTexture);
            DestroyTexture(navigationSelectedTexture);
            DestroyTexture(accentTexture);
            DestroyTexture(normalStatusTexture);
            DestroyTexture(cautionStatusTexture);
            DestroyTexture(criticalStatusTexture);
        }

        private void FrameBounds(Bounds bounds, float margin)
        {
            float aspect = Screen.height > 0
                ? Mathf.Max(0.1f, Screen.width / (float)Screen.height)
                : 16f / 9f;
            cameraRotation = Quaternion.Euler(ViewPitch, ViewYaw, 0f);
            float cameraDistance = Mathf.Max(80f, bounds.extents.magnitude * 3f);
            cameraPosition = bounds.center - cameraRotation * Vector3.forward * cameraDistance;
            orthographicSize = CalculateOrthographicSize(bounds, cameraRotation, aspect) * margin;
            farClipPlane = cameraDistance + bounds.extents.magnitude * 2f + 100f;
            viewReady = true;
            ApplyTopView();
        }

        private void InitializeLineStatuses()
        {
            for (int index = 0; index < lineStatuses.Length; index++)
            {
                lineStatuses[index] = FactoryStatus.Normal;
                lineDisplayNames[index] = GetFallbackLineDisplayName(index + 1);
            }
        }

        private void ApplyTopView()
        {
            if (topViewCamera == null)
                return;

            topViewCamera.transform.SetPositionAndRotation(
                cameraPosition,
                cameraRotation);
            topViewCamera.rect = new Rect(0f, 0f, 1f, 1f);
            topViewCamera.targetTexture = null;
            topViewCamera.orthographic = true;
            topViewCamera.orthographicSize = Mathf.Max(1f, orthographicSize);
            topViewCamera.nearClipPlane = 0.1f;
            topViewCamera.farClipPlane = Mathf.Max(101f, farClipPlane);
            topViewCamera.clearFlags = CameraClearFlags.SolidColor;
            topViewCamera.backgroundColor = new Color32(7, 17, 39, 255);
            topViewCamera.useOcclusionCulling = false;
            topViewCamera.cullingMask =
                (1 << TopViewLayer) |
                (1 << StructureLayer) |
                (1 << ActivityLayer);
            topViewCamera.enabled = true;
        }

        private void CacheLineMarkerPositions(Transform lineRoot)
        {
            for (int index = 0; index < LineCount; index++)
            {
                hasLineMarkerPosition[index] = false;
                Transform line = FindNumberedLine(lineRoot, index + 1);
                if (line == null ||
                    !TryCalculateBounds(line.GetComponentsInChildren<Renderer>(true), out Bounds bounds))
                {
                    continue;
                }

                lineMarkerWorldPositions[index] = bounds.center;
                hasLineMarkerPosition[index] = true;
            }

            AssignLineDisplayNamesByPosition();
        }

        private void AssignLineDisplayNamesByPosition()
        {
            List<int> validLines = new List<int>();
            for (int index = 0; index < LineCount; index++)
            {
                if (hasLineMarkerPosition[index])
                    validLines.Add(index);
                else
                    lineDisplayNames[index] = GetFallbackLineDisplayName(index + 1);
            }

            if (validLines.Count != LineCount)
                return;

            int[] columnByLine = new int[LineCount];
            int[] rowByLine = new int[LineCount];
            validLines.Sort((left, right) =>
                lineMarkerWorldPositions[left].x.CompareTo(lineMarkerWorldPositions[right].x));
            for (int rank = 0; rank < validLines.Count; rank++)
                columnByLine[validLines[rank]] = rank / 4;

            validLines.Sort((left, right) =>
                lineMarkerWorldPositions[right].z.CompareTo(lineMarkerWorldPositions[left].z));
            for (int rank = 0; rank < validLines.Count; rank++)
                rowByLine[validLines[rank]] = rank / 4 + 1;

            for (int index = 0; index < LineCount; index++)
            {
                char column = (char)('A' + Mathf.Clamp(columnByLine[index], 0, 3));
                lineDisplayNames[index] = $"{column}{Mathf.Clamp(rowByLine[index], 1, 4)}";
            }
        }

        private static string GetFallbackLineDisplayName(int lineNumber)
        {
            int zeroBased = Mathf.Clamp(lineNumber - 1, 0, LineCount - 1);
            char column = (char)('A' + zeroBased % 4);
            int row = zeroBased / 4 + 1;
            return $"{column}{row}";
        }

        public string GetLineDisplayName(int lineNumber)
        {
            if (lineNumber < 1 || lineNumber > LineCount)
                return GetFallbackLineDisplayName(lineNumber);

            string displayName = lineDisplayNames[lineNumber - 1];
            return string.IsNullOrEmpty(displayName)
                ? GetFallbackLineDisplayName(lineNumber)
                : displayName;
        }

        private void DrawFactoryStatusMarkers()
        {
            if (topViewCamera == null)
                return;

            for (int index = 0; index < LineCount; index++)
            {
                if (!hasLineMarkerPosition[index])
                    continue;

                Vector3 screenPoint = topViewCamera.WorldToScreenPoint(lineMarkerWorldPositions[index]);
                if (screenPoint.z <= 0f)
                    continue;

                Vector2 guiPoint = new Vector2(screenPoint.x, Screen.height - screenPoint.y);
                if (guiPoint.x < -StatusMarkerSize || guiPoint.x > Screen.width + StatusMarkerSize ||
                    guiPoint.y < -StatusMarkerSize || guiPoint.y > Screen.height + StatusMarkerSize)
                {
                    continue;
                }

                FactoryStatus status = lineStatuses[index];
                Texture2D statusTexture = GetStatusTexture(status);
                Rect marker = new Rect(
                    guiPoint.x - StatusMarkerSize * 0.5f,
                    guiPoint.y - StatusMarkerSize * 0.5f,
                    StatusMarkerSize,
                    StatusMarkerSize);
                Rect glow = new Rect(marker.x - 10f, marker.y - 10f,
                    marker.width + 20f, marker.height + 20f);

                Color previousColor = GUI.color;
                GUI.color = new Color(1f, 1f, 1f, 0.24f);
                GUI.DrawTexture(glow, statusTexture, ScaleMode.ScaleToFit, true);
                GUI.color = Color.white;
                GUI.DrawTexture(marker, statusTexture, ScaleMode.ScaleToFit, true);
                GUI.color = previousColor;

                Rect clickArea = new Rect(marker.x - 7f, marker.y - 7f,
                    marker.width + 14f, marker.height + 28f);
                if (GUI.Button(
                        clickArea,
                        new GUIContent(string.Empty,
                            $"라인 {GetLineDisplayName(index + 1)} - {GetStatusText(status)}"),
                        mapMarkerButtonStyle))
                {
                    SelectLine(index + 1);
                }

                Rect label = new Rect(marker.center.x - 27f, marker.yMax + 1f, 54f, 18f);
                DrawColorRect(label, new Color32(8, 18, 33, 220));
                mapMarkerLabelStyle.normal.textColor = GetStatusColor(status);
                GUI.Label(label, GetLineDisplayName(index + 1), mapMarkerLabelStyle);
            }
        }

        private void DrawLineNavigation()
        {
            Rect panel = new Rect(0f, 0f, NavigationWidth, Screen.height);
            GUI.DrawTexture(panel, navigationPanelTexture, ScaleMode.StretchToFill, true);

            GUI.Label(new Rect(14f, 10f, NavigationWidth - 28f, 30f),
                "공장 라인 모니터링", navigationTitleStyle);

            FactoryStatus factoryStatus = GetFactoryStatus();
            Rect factoryCard = new Rect(10f, 45f, NavigationWidth - 20f, 56f);
            GUI.DrawTexture(factoryCard, navigationRowTexture, ScaleMode.StretchToFill, true);
            GUI.DrawTexture(new Rect(factoryCard.x + 12f, factoryCard.y + 15f, 26f, 26f),
                GetStatusTexture(factoryStatus), ScaleMode.ScaleToFit, true);
            GUI.Label(new Rect(factoryCard.x + 48f, factoryCard.y + 5f, 105f, 22f),
                "공장 상태", navigationLabelStyle);
            statusLabelStyle.normal.textColor = GetStatusColor(factoryStatus);
            GUI.Label(new Rect(factoryCard.x + 48f, factoryCard.y + 26f, 105f, 24f),
                GetStatusText(factoryStatus), statusLabelStyle);

            DrawStatusLegend(new Rect(10f, 107f, NavigationWidth - 20f, 25f));

            Rect allButton = new Rect(10f, 137f, NavigationWidth - 20f, 38f);
            GUIStyle allStyle = selectedLineNumber == 0
                ? selectedNavigationButtonStyle
                : navigationButtonStyle;
            if (GUI.Button(allButton, "전체 라인 보기", allStyle))
                SelectLine(0);
            if (selectedLineNumber == 0)
                GUI.DrawTexture(new Rect(allButton.x, allButton.y, 3f, allButton.height), accentTexture);

            Rect viewport = new Rect(8f, 183f, NavigationWidth - 12f, Mathf.Max(40f, Screen.height - 239f));
            const float rowHeight = 38f;
            const float rowGap = 4f;
            float contentHeight = LineCount * (rowHeight + rowGap) - rowGap;
            Rect content = new Rect(0f, 0f, viewport.width - 18f, contentHeight);
            lineScrollPosition = GUI.BeginScrollView(viewport, lineScrollPosition, content, false, false);

            float y = 0f;
            for (int displayIndex = 0; displayIndex < LineCount; displayIndex++)
            {
                int lineNumber = GetLineNumberAtNavigationIndex(displayIndex);
                FactoryStatus status = lineStatuses[lineNumber - 1];
                Rect row = new Rect(0f, y, content.width, rowHeight);
                GUIStyle rowStyle = selectedLineNumber == lineNumber
                    ? selectedNavigationButtonStyle
                    : navigationButtonStyle;

                if (GUI.Button(row, $"라인 {GetLineDisplayName(lineNumber)}", rowStyle))
                    SelectLine(lineNumber);

                GUI.DrawTexture(new Rect(row.x + 10f, row.y + 11f, 16f, 16f),
                    GetStatusTexture(status), ScaleMode.ScaleToFit, true);
                statusLabelStyle.normal.textColor = GetStatusColor(status);
                GUI.Label(new Rect(row.xMax - 52f, row.y + 7f, 44f, 24f),
                    GetStatusText(status), statusLabelStyle);

                if (selectedLineNumber == lineNumber)
                    GUI.DrawTexture(new Rect(row.x, row.y, 3f, row.height), accentTexture);

                y += rowHeight + rowGap;
            }

            GUI.EndScrollView();
            GUI.Label(new Rect(10f, Screen.height - 50f, NavigationWidth - 20f, 18f),
                "시점 변경", legendStyle);
            DrawViewModeButtons(new Rect(10f, Screen.height - 32f, NavigationWidth - 20f, 28f));
        }

        private int GetLineNumberAtNavigationIndex(int displayIndex)
        {
            int columnIndex = Mathf.Clamp(displayIndex / 4, 0, 3);
            int rowNumber = Mathf.Clamp(displayIndex % 4 + 1, 1, 4);
            string targetDisplayName = $"{(char)('A' + columnIndex)}{rowNumber}";

            for (int lineNumber = 1; lineNumber <= LineCount; lineNumber++)
            {
                if (GetLineDisplayName(lineNumber) == targetDisplayName)
                    return lineNumber;
            }

            return (rowNumber - 1) * 4 + columnIndex + 1;
        }

        private void DrawViewModeButtons(Rect rect)
        {
            const float gap = 4f;
            float buttonWidth = (rect.width - gap) * 0.5f;

            if (GUI.Button(new Rect(rect.x, rect.y, buttonWidth, rect.height),
                    "탑뷰", selectedViewModeButtonStyle))
            {
                SelectLine(0);
            }

            if (GUI.Button(new Rect(rect.x + buttonWidth + gap, rect.y, buttonWidth, rect.height),
                    "자유 시점", viewModeButtonStyle))
            {
                SceneManager.LoadScene("FactorySceneSample", LoadSceneMode.Single);
            }
        }

        private void DrawStatusLegend(Rect rect)
        {
            FactoryStatus[] statuses =
            {
                FactoryStatus.Normal,
                FactoryStatus.Caution,
                FactoryStatus.Critical
            };

            float itemWidth = rect.width / statuses.Length;
            for (int index = 0; index < statuses.Length; index++)
            {
                Rect item = new Rect(rect.x + itemWidth * index, rect.y, itemWidth, rect.height);
                GUI.DrawTexture(new Rect(item.x + 2f, item.y + 6f, 12f, 12f),
                    GetStatusTexture(statuses[index]), ScaleMode.ScaleToFit, true);
                GUI.Label(new Rect(item.x + 17f, item.y, item.width - 17f, item.height),
                    GetStatusText(statuses[index]), legendStyle);
            }
        }

        public void SelectLine(int lineNumber)
        {
            selectedLineNumber = Mathf.Clamp(lineNumber, 0, LineCount);

            if (selectedLineNumber == 0)
            {
                if (hasFactoryViewBounds)
                    FrameBounds(factoryViewBounds, ViewMargin);
                return;
            }

            Transform lineRoot = FindLineRoot(SceneManager.GetActiveScene());
            Transform selectedLine = FindNumberedLine(lineRoot, selectedLineNumber);
            if (selectedLine != null &&
                TryCalculateBounds(selectedLine.GetComponentsInChildren<Renderer>(true), out Bounds lineBounds))
            {
                FrameBounds(lineBounds, 1.42f);
            }

            DigitalTwin.Line11.Line11PressDetailController detailController =
                DigitalTwin.Line11.Line11PressDetailController.Instance;
            if (detailController != null)
            {
                if (selectedLineNumber == 11)
                    detailController.OpenDetail();
                else
                    detailController.OpenLineDetail(selectedLineNumber);
            }
        }

        private static Transform FindNumberedLine(Transform lineRoot, int lineNumber)
        {
            if (lineRoot == null)
                return null;

            string targetName = lineNumber.ToString("00");
            foreach (Transform child in lineRoot)
            {
                if (child.name == targetName)
                    return child;
            }

            return null;
        }

        public FactoryStatus GetLineStatus(int lineNumber)
        {
            if (lineNumber < 1 || lineNumber > LineCount)
                return FactoryStatus.Normal;

            return lineStatuses[lineNumber - 1];
        }

        public FactoryStatus GetFactoryStatus()
        {
            FactoryStatus result = FactoryStatus.Normal;
            foreach (FactoryStatus status in lineStatuses)
            {
                if (status > result)
                    result = status;
            }

            return result;
        }

        private Texture2D GetStatusTexture(FactoryStatus status)
        {
            switch (status)
            {
                case FactoryStatus.Critical:
                    return criticalStatusTexture;
                case FactoryStatus.Caution:
                    return cautionStatusTexture;
                default:
                    return normalStatusTexture;
            }
        }

        private static Color GetStatusColor(FactoryStatus status)
        {
            switch (status)
            {
                case FactoryStatus.Critical:
                    return new Color32(235, 77, 77, 255);
                case FactoryStatus.Caution:
                    return new Color32(238, 159, 56, 255);
                default:
                    return new Color32(64, 210, 137, 255);
            }
        }

        private static string GetStatusText(FactoryStatus status)
        {
            switch (status)
            {
                case FactoryStatus.Critical:
                    return "이상";
                case FactoryStatus.Caution:
                    return "주의";
                default:
                    return "정상";
            }
        }

        private void EnsureNavigationStyles()
        {
            if (navigationTitleStyle != null && navigationButtonStyle != null &&
                normalStatusTexture != null && mapMarkerLabelStyle != null &&
                mapMarkerButtonStyle != null && viewModeButtonStyle != null &&
                selectedViewModeButtonStyle != null)
            {
                return;
            }

            Font font = Font.CreateDynamicFontFromOSFont(
                new[] { "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans CJK KR", "Arial" },
                16);
            if (font == null)
                font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");

            navigationPanelTexture = CreateSolidTexture(new Color32(10, 19, 39, 244));
            navigationRowTexture = CreateSolidTexture(new Color32(19, 35, 60, 245));
            navigationHoverTexture = CreateSolidTexture(new Color32(24, 67, 96, 255));
            navigationSelectedTexture = CreateSolidTexture(new Color32(19, 82, 113, 255));
            accentTexture = CreateSolidTexture(new Color32(24, 180, 224, 255));
            normalStatusTexture = CreateCircleTexture(GetStatusColor(FactoryStatus.Normal));
            cautionStatusTexture = CreateCircleTexture(GetStatusColor(FactoryStatus.Caution));
            criticalStatusTexture = CreateCircleTexture(GetStatusColor(FactoryStatus.Critical));

            navigationTitleStyle = new GUIStyle(GUI.skin.label)
            {
                font = font,
                fontSize = 17,
                fontStyle = FontStyle.Bold,
                alignment = TextAnchor.MiddleLeft
            };
            navigationTitleStyle.normal.textColor = new Color32(230, 240, 248, 255);

            navigationLabelStyle = new GUIStyle(navigationTitleStyle)
            {
                fontSize = 12,
                fontStyle = FontStyle.Normal
            };
            navigationLabelStyle.normal.textColor = new Color32(153, 178, 197, 255);

            statusLabelStyle = new GUIStyle(navigationTitleStyle)
            {
                fontSize = 16,
                alignment = TextAnchor.MiddleLeft
            };

            legendStyle = new GUIStyle(navigationLabelStyle)
            {
                fontSize = 11,
                alignment = TextAnchor.MiddleLeft
            };

            navigationButtonStyle = new GUIStyle(GUI.skin.button)
            {
                font = font,
                fontSize = 14,
                fontStyle = FontStyle.Bold,
                alignment = TextAnchor.MiddleLeft,
                padding = new RectOffset(34, 52, 0, 0)
            };
            navigationButtonStyle.normal.background = navigationRowTexture;
            navigationButtonStyle.hover.background = navigationHoverTexture;
            navigationButtonStyle.active.background = navigationSelectedTexture;
            navigationButtonStyle.normal.textColor = new Color32(226, 235, 242, 255);
            navigationButtonStyle.hover.textColor = Color.white;
            navigationButtonStyle.active.textColor = Color.white;

            selectedNavigationButtonStyle = new GUIStyle(navigationButtonStyle);
            selectedNavigationButtonStyle.normal.background = navigationSelectedTexture;

            viewModeButtonStyle = new GUIStyle(navigationButtonStyle)
            {
                fontSize = 12,
                alignment = TextAnchor.MiddleCenter,
                padding = new RectOffset(2, 2, 0, 0)
            };
            selectedViewModeButtonStyle = new GUIStyle(viewModeButtonStyle);
            selectedViewModeButtonStyle.normal.background = navigationSelectedTexture;
            selectedViewModeButtonStyle.normal.textColor = Color.white;

            mapMarkerLabelStyle = new GUIStyle(navigationTitleStyle)
            {
                fontSize = 12,
                alignment = TextAnchor.MiddleCenter
            };
            mapMarkerButtonStyle = new GUIStyle(GUIStyle.none);
        }

        private static Texture2D CreateSolidTexture(Color color)
        {
            Texture2D texture = new Texture2D(1, 1, TextureFormat.RGBA32, false)
            {
                hideFlags = HideFlags.HideAndDontSave
            };
            texture.SetPixel(0, 0, color);
            texture.Apply();
            return texture;
        }

        private static void DrawColorRect(Rect rect, Color color)
        {
            Color previousColor = GUI.color;
            GUI.color = color;
            GUI.DrawTexture(rect, Texture2D.whiteTexture);
            GUI.color = previousColor;
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
                    texture.SetPixel(x, y, new Color(color.r, color.g, color.b, color.a * alpha));
                }
            }

            texture.Apply();
            return texture;
        }

        private static void DestroyTexture(Texture2D texture)
        {
            if (texture != null)
                Destroy(texture);
        }

        private static float CalculateOrthographicSize(Bounds bounds, Quaternion rotation, float aspect)
        {
            Vector3 cameraRight = rotation * Vector3.right;
            Vector3 cameraUp = rotation * Vector3.up;
            Vector3 extents = bounds.extents;
            float maximumHorizontal = 0f;
            float maximumVertical = 0f;

            for (int x = -1; x <= 1; x += 2)
            {
                for (int y = -1; y <= 1; y += 2)
                {
                    for (int z = -1; z <= 1; z += 2)
                    {
                        Vector3 cornerOffset = new Vector3(
                            extents.x * x,
                            extents.y * y,
                            extents.z * z);
                        maximumHorizontal = Mathf.Max(
                            maximumHorizontal,
                            Mathf.Abs(Vector3.Dot(cornerOffset, cameraRight)));
                        maximumVertical = Mathf.Max(
                            maximumVertical,
                            Mathf.Abs(Vector3.Dot(cornerOffset, cameraUp)));
                    }
                }
            }

            return Mathf.Max(maximumVertical, maximumHorizontal / Mathf.Max(0.1f, aspect));
        }

        private static void AssignLayer(Renderer[] renderers, int layer)
        {
            foreach (Renderer renderer in renderers)
            {
                if (renderer != null && renderer.gameObject.layer != PreviewLayer)
                    renderer.gameObject.layer = layer;
            }
        }

        /// <summary>
        /// Hides complete scene objects that touch or cross the requested
        /// world-space height. This also removes floor-to-ceiling pillars and
        /// their overhead connectors instead of leaving the lower pillar visible.
        /// </summary>
        private static int HideRenderersAboveWorldHeight(Scene scene, float maximumHeight)
        {
            int hiddenCount = 0;
            Renderer[] sceneRenderers = FindObjectsByType<Renderer>(FindObjectsInactive.Include);

            foreach (Renderer renderer in sceneRenderers)
            {
                if (renderer == null || !renderer.enabled ||
                    renderer.gameObject.scene != scene ||
                    renderer.gameObject.layer == PreviewLayer)
                {
                    continue;
                }

                Bounds bounds = renderer.bounds;
                if (!IsFinite(bounds.center) || !IsFinite(bounds.extents) ||
                    bounds.max.y < maximumHeight)
                    continue;

                renderer.enabled = false;
                hiddenCount++;
            }

            return hiddenCount;
        }

        private static Renderer[] CollectStructureRenderers(Scene scene, out Transform ceilingRoot)
        {
            ceilingRoot = FindSceneTransform(scene, "Ceiling");
            Transform floorRoot = FindSceneTransform(scene, "Floor");
            Transform buildingRoot = FindSceneTransform(scene, "Building");
            List<Renderer> renderers = new List<Renderer>();

            AddUniqueRenderers(buildingRoot, renderers);
            AddUniqueRenderers(floorRoot, renderers);
            AddUniqueRenderers(ceilingRoot, renderers);
            return renderers.ToArray();
        }

        private static Renderer[] CollectActivityRenderers(Scene scene)
        {
            List<Renderer> renderers = new List<Renderer>();
            AddUniqueRenderers(FindSceneTransform(scene, "Animation Sample"), renderers);
            AddUniqueRenderers(FindSceneTransform(scene, "Prop"), renderers);
            AddUniqueRenderers(FindSceneTransform(scene, "Controller"), renderers);
            return renderers.ToArray();
        }

        private static int RemoveWallWindowVisuals(Scene scene, Transform buildingRoot)
        {
            int removedCount = 0;
            if (buildingRoot != null)
            {
                foreach (Renderer renderer in buildingRoot.GetComponentsInChildren<Renderer>(true))
                {
                    if (renderer == null || !renderer.enabled || !IsWallWindowRenderer(renderer))
                        continue;

                    renderer.enabled = false;
                    removedCount++;
                }
            }

            foreach (Light sceneLight in FindObjectsByType<Light>(FindObjectsInactive.Include))
            {
                if (sceneLight == null || sceneLight.gameObject.scene != scene)
                    continue;

                if (ContainsWindowName(sceneLight.transform))
                    sceneLight.enabled = false;
            }

            return removedCount;
        }

        private static bool IsWallWindowRenderer(Renderer renderer)
        {
            if (ContainsWindowName(renderer.transform))
                return true;

            int materialCount = 0;
            int windowMaterialCount = 0;
            foreach (Material material in renderer.sharedMaterials)
            {
                if (material == null)
                    continue;

                materialCount++;
                string materialName = material.name;
                if (materialName.IndexOf("Window", System.StringComparison.OrdinalIgnoreCase) >= 0 ||
                    materialName.IndexOf("Glass", System.StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    windowMaterialCount++;
                }
            }

            // Do not disable a combined wall mesh merely because one of its
            // sub-materials is glass. Named window objects are handled above.
            return materialCount > 0 && windowMaterialCount == materialCount;
        }

        private static bool ContainsWindowName(Transform transform)
        {
            Transform candidate = transform;
            while (candidate != null)
            {
                string objectName = candidate.name;
                if (objectName.IndexOf("Window", System.StringComparison.OrdinalIgnoreCase) >= 0 ||
                    objectName.IndexOf("Glass", System.StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    return true;
                }

                if (objectName == "Building")
                    break;

                candidate = candidate.parent;
            }

            return false;
        }

        private static void AddUniqueRenderers(Transform root, List<Renderer> destination)
        {
            if (root == null)
                return;

            foreach (Renderer renderer in root.GetComponentsInChildren<Renderer>(true))
            {
                if (renderer != null && !destination.Contains(renderer))
                    destination.Add(renderer);
            }
        }

        private static int PrepareCeilingForTopView(
            Renderer[] renderers,
            Transform ceilingRoot,
            Transform ceilingFixtureRoot)
        {
            int removedCount = 0;
            foreach (Renderer renderer in renderers)
            {
                if (renderer == null)
                    continue;

                bool belongsToCeiling = ceilingRoot != null &&
                    (renderer.transform == ceilingRoot || renderer.transform.IsChildOf(ceilingRoot));
                bool belongsToCeilingFixtures = ceilingFixtureRoot != null &&
                    (renderer.transform == ceilingFixtureRoot || renderer.transform.IsChildOf(ceilingFixtureRoot));
                bool isCeilingSurface = IsCeilingSurfaceRenderer(renderer, ceilingRoot);
                bool isNamedCeilingAttachment = IsNamedCeilingAttachment(renderer);

                // Preserve only the ceiling/roof surface. Fixtures, frames, ducts,
                // lamps, and every other renderer parented below Ceiling are removed
                // from the top-view presentation.
                if ((belongsToCeiling || belongsToCeilingFixtures || isNamedCeilingAttachment) &&
                    !isCeilingSurface)
                {
                    renderer.enabled = false;
                    removedCount++;
                    continue;
                }

                if (!isCeilingSurface)
                    continue;

                renderer.shadowCastingMode = ShadowCastingMode.Off;
                renderer.receiveShadows = false;
                Material[] materials = renderer.materials;
                foreach (Material material in materials)
                    ConfigureTransparentMaterial(material, CeilingOpacity);
            }

            if (ceilingRoot != null)
            {
                foreach (Light ceilingLight in ceilingRoot.GetComponentsInChildren<Light>(true))
                    ceilingLight.enabled = false;
            }

            if (ceilingFixtureRoot != null && ceilingFixtureRoot != ceilingRoot)
            {
                foreach (Light ceilingLight in ceilingFixtureRoot.GetComponentsInChildren<Light>(true))
                    ceilingLight.enabled = false;
            }

            return removedCount;
        }

        private static bool IsNamedCeilingAttachment(Renderer renderer)
        {
            // Building.fbx stores the ceiling systems directly below Building,
            // not below the scene's Ceiling transform. Their imported names use
            // the CL_ prefix (CL_Truss, CL_Pipe, CL_Duct, CL_SprinklerPipe,
            // CL_LightBase, etc.). Keep CL_Ceiling itself and remove the rest.
            Transform candidate = renderer.transform;
            while (candidate != null)
            {
                string objectName = candidate.name;
                if (objectName.StartsWith("CL_", System.StringComparison.OrdinalIgnoreCase))
                {
                    return objectName.IndexOf(
                        "Ceiling",
                        System.StringComparison.OrdinalIgnoreCase) < 0;
                }

                if (objectName == "Building")
                    break;

                candidate = candidate.parent;
            }

            return false;
        }

        private static int RemoveCeilingLightVisuals(Scene scene)
        {
            int removedCount = 0;
            Renderer[] sceneRenderers = FindObjectsByType<Renderer>(
                FindObjectsInactive.Include,
                FindObjectsSortMode.None);

            foreach (Renderer renderer in sceneRenderers)
            {
                if (renderer == null || renderer.gameObject.scene != scene ||
                    !IsCeilingLightVisual(renderer))
                {
                    continue;
                }

                if (renderer.enabled)
                {
                    renderer.enabled = false;
                    removedCount++;
                }
            }

            Light[] sceneLights = FindObjectsByType<Light>(
                FindObjectsInactive.Include,
                FindObjectsSortMode.None);
            foreach (Light sceneLight in sceneLights)
            {
                if (sceneLight != null && sceneLight.gameObject.scene == scene &&
                    IsCeilingLightTransform(sceneLight.transform))
                {
                    sceneLight.enabled = false;
                }
            }

            return removedCount;
        }

        private static int RemoveLongHorizontalStructureLines(
            Scene scene,
            Bounds lineBounds)
        {
            int removedCount = 0;
            float minimumLineWidth = lineBounds.size.x * 0.50f;
            float minimumLineDepth = lineBounds.size.z * 0.50f;
            Renderer[] sceneRenderers = FindObjectsByType<Renderer>(FindObjectsInactive.Include);

            foreach (Renderer renderer in sceneRenderers)
            {
                if (renderer == null || !renderer.enabled ||
                    renderer.gameObject.scene != scene ||
                    renderer.gameObject.layer == PreviewLayer ||
                    IsCeilingSurfaceRenderer(renderer, null))
                {
                    continue;
                }

                Bounds bounds = renderer.bounds;
                if (!IsFinite(bounds.center) || !IsFinite(bounds.extents))
                    continue;

                bool runsAlongX = bounds.size.x >= bounds.size.z;
                float length = runsAlongX ? bounds.size.x : bounds.size.z;
                float horizontalThickness = runsAlongX ? bounds.size.z : bounds.size.x;
                float crossSection = Mathf.Max(
                    0.01f,
                    Mathf.Max(bounds.size.y, horizontalThickness));
                bool spansFactory = runsAlongX
                    ? bounds.size.x >= minimumLineWidth
                    : bounds.size.z >= minimumLineDepth;
                bool isThinLongShape = length >= crossSection * FloatingBeamAspectRatio;
                bool isElevatedFloatingBeam =
                    bounds.center.y >= FloatingBeamMinimumHeight &&
                    length >= FloatingBeamMinimumLength;

                if (!isThinLongShape || (!spansFactory && !isElevatedFloatingBeam))
                    continue;

                renderer.enabled = false;
                removedCount++;
            }

            return removedCount;
        }

        private static bool IsCeilingLightVisual(Renderer renderer)
        {
            if (IsCeilingLightTransform(renderer.transform))
                return true;

            foreach (Material material in renderer.sharedMaterials)
            {
                if (material == null)
                    continue;

                string materialName = material.name;
                if (materialName.Equals("Light", System.StringComparison.OrdinalIgnoreCase) ||
                    materialName.StartsWith("LightBase", System.StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool IsCeilingLightTransform(Transform transform)
        {
            Transform candidate = transform;
            while (candidate != null)
            {
                string objectName = candidate.name;
                if (objectName.StartsWith("Light_2F", System.StringComparison.OrdinalIgnoreCase) ||
                    objectName.StartsWith("CL_Light", System.StringComparison.OrdinalIgnoreCase) ||
                    objectName.Equals("Light", System.StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }

                candidate = candidate.parent;
            }

            return false;
        }

        private static bool IsCeilingSurfaceRenderer(Renderer renderer, Transform ceilingRoot)
        {
            if (ceilingRoot != null && renderer.transform == ceilingRoot)
                return true;

            if (renderer.name.IndexOf("Ceiling", System.StringComparison.OrdinalIgnoreCase) >= 0 ||
                renderer.name.IndexOf("Roof", System.StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return true;
            }

            foreach (Material material in renderer.sharedMaterials)
            {
                if (material != null &&
                    material.name.IndexOf("Ceiling", System.StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    return true;
                }
            }

            return false;
        }

        private static void ConfigureTransparentMaterial(Material material, float opacity)
        {
            if (material == null)
                return;

            Color color = Color.white;
            if (material.HasProperty("_BaseColor"))
                color = material.GetColor("_BaseColor");
            else if (material.HasProperty("_Color"))
                color = material.GetColor("_Color");

            color.a = opacity;
            if (material.HasProperty("_BaseColor"))
                material.SetColor("_BaseColor", color);
            if (material.HasProperty("_Color"))
                material.SetColor("_Color", color);
            if (material.HasProperty("_SurfaceType"))
                material.SetFloat("_SurfaceType", 1f);
            if (material.HasProperty("_BlendMode"))
                material.SetFloat("_BlendMode", 0f);
            if (material.HasProperty("_SrcBlend"))
                material.SetFloat("_SrcBlend", (float)BlendMode.SrcAlpha);
            if (material.HasProperty("_DstBlend"))
                material.SetFloat("_DstBlend", (float)BlendMode.OneMinusSrcAlpha);
            if (material.HasProperty("_ZWrite"))
                material.SetFloat("_ZWrite", 0f);
            if (material.HasProperty("_TransparentZWrite"))
                material.SetFloat("_TransparentZWrite", 0f);

            material.SetOverrideTag("RenderType", "Transparent");
            material.EnableKeyword("_SURFACE_TYPE_TRANSPARENT");
            material.DisableKeyword("_SURFACE_TYPE_OPAQUE");
            material.renderQueue = (int)RenderQueue.Transparent;
        }

        private void ConfigureFactoryLighting(Scene scene, Bounds factoryBounds)
        {
            if (factoryLightingAdjusted)
                return;

            int disabledWallLights = 0;
            int dimmedInteriorLights = 0;
            Light[] lights = FindObjectsByType<Light>(FindObjectsInactive.Include, FindObjectsSortMode.None);
            foreach (Light factoryLight in lights)
            {
                if (factoryLight == null || factoryLight.gameObject.scene != scene)
                    continue;

                if (IsNearFactoryWall(factoryLight.transform.position, factoryBounds))
                {
                    factoryLight.enabled = false;
                    disabledWallLights++;
                }
                else
                {
                    factoryLight.intensity *= FactoryLightMultiplier;
                    dimmedInteriorLights++;
                }
            }

            factoryLightingAdjusted = true;
            Debug.Log(
                $"[탑뷰] 벽 주변 라이트 {disabledWallLights}개를 끄고 " +
                $"내부 라이트 {dimmedInteriorLights}개를 50% 밝기로 조정했습니다.");
        }

        private static bool IsNearFactoryWall(Vector3 lightPosition, Bounds factoryBounds)
        {
            float distanceToXWall = Mathf.Min(
                Mathf.Abs(lightPosition.x - factoryBounds.min.x),
                Mathf.Abs(lightPosition.x - factoryBounds.max.x));
            float distanceToZWall = Mathf.Min(
                Mathf.Abs(lightPosition.z - factoryBounds.min.z),
                Mathf.Abs(lightPosition.z - factoryBounds.max.z));

            bool withinXSpan =
                lightPosition.x >= factoryBounds.min.x - WallLightBand &&
                lightPosition.x <= factoryBounds.max.x + WallLightBand;
            bool withinZSpan =
                lightPosition.z >= factoryBounds.min.z - WallLightBand &&
                lightPosition.z <= factoryBounds.max.z + WallLightBand;

            return
                (distanceToXWall <= WallLightBand && withinZSpan) ||
                (distanceToZWall <= WallLightBand && withinXSpan);
        }

        private static Transform FindLineRoot(Scene scene)
        {
            return FindSceneTransform(scene, LineRootName);
        }

        private static Transform FindSceneTransform(Scene scene, string objectName)
        {
            foreach (GameObject rootObject in scene.GetRootGameObjects())
            {
                if (rootObject.name == objectName)
                    return rootObject.transform;
            }

            Transform[] transforms = FindObjectsByType<Transform>(FindObjectsInactive.Include, FindObjectsSortMode.None);
            foreach (Transform candidate in transforms)
            {
                if (candidate.gameObject.scene == scene && candidate.name == objectName)
                    return candidate;
            }

            return null;
        }

        private static Camera FindSceneCamera(Scene scene)
        {
            Camera mainCamera = Camera.main;
            if (mainCamera != null && mainCamera.gameObject.scene == scene && mainCamera.targetTexture == null)
                return mainCamera;

            Camera[] cameras = FindObjectsByType<Camera>(FindObjectsInactive.Include, FindObjectsSortMode.None);
            foreach (Camera candidate in cameras)
            {
                if (candidate.gameObject.scene == scene && candidate.targetTexture == null)
                    return candidate;
            }

            return null;
        }

        private static bool TryCalculateBounds(Renderer[] renderers, out Bounds bounds)
        {
            bounds = default;
            bool hasBounds = false;

            foreach (Renderer renderer in renderers)
            {
                if (renderer == null || !renderer.enabled || renderer.gameObject.layer == PreviewLayer)
                    continue;

                Bounds rendererBounds = renderer.bounds;
                if (!IsFinite(rendererBounds.center) || !IsFinite(rendererBounds.extents))
                    continue;

                if (!hasBounds)
                {
                    bounds = rendererBounds;
                    hasBounds = true;
                }
                else
                {
                    bounds.Encapsulate(rendererBounds);
                }
            }

            return hasBounds;
        }

        private static bool IsFinite(Vector3 value)
        {
            return float.IsFinite(value.x) && float.IsFinite(value.y) && float.IsFinite(value.z);
        }
    }
}
