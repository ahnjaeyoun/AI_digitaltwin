using UnityEngine;

namespace DigitalTwin.LineView
{
    /// <summary>
    /// 화면 우측 하단에 4x4 형태로 라인 버튼을 그려주는 UI 스크립트입니다.
    /// </summary>
    public class LineSelectorUI : MonoBehaviour
    {
        private GUIStyle buttonStyle;
        private GUIStyle containerStyle;
        private bool isStyleInitialized = false;

        private readonly string[] rowLabels = { "1", "2", "3", "4" };
        private readonly string[] colLabels = { "A", "B", "C", "D" };

        private void OnGUI()
        {
            DigitalTwin.Line11.Line11PressDetailController detailController =
                DigitalTwin.Line11.Line11PressDetailController.Instance;
            if (detailController != null && detailController.IsOpen)
                return;

            InitStyles();

            float screenWidth = Screen.width;
            float screenHeight = Screen.height;
            
            // 4x4 그리드 패널 크기 및 간격 설정
            float padding = 12f;
            float gap = 6f;
            float buttonSize = 54f; // 터치/클릭하기 좋은 크기
            
            float panelWidth = (padding * 2) + (buttonSize * 4) + (gap * 3);
            float panelHeight = (padding * 2) + (buttonSize * 4) + (gap * 3);
            
            // 화면 우측 하단 배치 (화면 끝에서 30f 만큼 여백)
            float marginX = 30f;
            float marginY = 30f;
            Rect panelRect = new Rect(screenWidth - panelWidth - marginX, screenHeight - panelHeight - marginY, panelWidth, panelHeight);
            
            GUI.Box(panelRect, "", containerStyle);

            for (int r = 0; r < 4; r++)
            {
                for (int c = 0; c < 4; c++)
                {
                    // 기존 Element 할당 방식(A1=0, A2=1, B1=4 ...)과 맞추기 위한 매핑
                    int arrayIndex = c * 4 + r;
                    string btnLabel = $"{colLabels[c]}{rowLabels[r]}"; // ex) A1, B1, C1...
                    
                    float btnX = panelRect.x + padding + c * (buttonSize + gap);
                    float btnY = panelRect.y + padding + r * (buttonSize + gap);
                    Rect btnRect = new Rect(btnX, btnY, buttonSize, buttonSize);
                    
                    if (GUI.Button(btnRect, btnLabel, buttonStyle))
                    {
                        if (LineViewManager.Instance != null)
                        {
                            LineViewManager.Instance.MoveToLine(arrayIndex);
                        }
                    }
                }
            }
        }

        private void InitStyles()
        {
            if (isStyleInitialized) return;
            isStyleInitialized = true;

            containerStyle = new GUIStyle(GUI.skin.box);
            containerStyle.normal.background = MakeTex(2, 2, new Color(0.1f, 0.12f, 0.15f, 0.85f)); // 어두운 남색 반투명

            buttonStyle = new GUIStyle(GUI.skin.button);
            buttonStyle.fontSize = 18;
            buttonStyle.fontStyle = FontStyle.Bold;
            buttonStyle.normal.textColor = Color.white;
            buttonStyle.hover.textColor = new Color(1f, 0.9f, 0.3f); // 마우스 오버 시 노란색
            
            buttonStyle.normal.background = MakeTex(2, 2, new Color(0.2f, 0.25f, 0.3f, 1f));
            buttonStyle.hover.background = MakeTex(2, 2, new Color(0.3f, 0.35f, 0.4f, 1f));
            buttonStyle.active.background = MakeTex(2, 2, new Color(0.1f, 0.6f, 0.8f, 1f)); // 클릭 시 강조색 (파랑)
        }

        private Texture2D MakeTex(int width, int height, Color col)
        {
            Color[] pix = new Color[width * height];
            for (int i = 0; i < pix.Length; ++i)
            {
                pix[i] = col;
            }
            Texture2D result = new Texture2D(width, height);
            result.SetPixels(pix);
            result.Apply();
            return result;
        }
    }
}
