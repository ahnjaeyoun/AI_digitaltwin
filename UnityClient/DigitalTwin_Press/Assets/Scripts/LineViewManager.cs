using UnityEngine;
using UnityFactorySceneHDRP;

namespace DigitalTwin.LineView
{
    /// <summary>
    /// 1~16번 라인의 관전 위치를 관리하는 매니저 클래스입니다.
    /// </summary>
    public class LineViewManager : MonoBehaviour
    {
        public static LineViewManager Instance { get; private set; }

        [Tooltip("1번부터 16번 라인의 관전 위치 배열입니다. (인스펙터에서 할당 가능)")]
        public Transform[] lineViewPoints = new Transform[16];

        private CameraMove playerCameraMove;

        private void Awake()
        {
            if (Instance != null && Instance != this)
            {
                Destroy(gameObject);
                return;
            }
            Instance = this;
            
            // 기존 씬에 존재하는 CameraMove 찾기
            playerCameraMove = FindAnyObjectByType<CameraMove>();
        }

        public void MoveToLine(int lineIndex)
        {
            if (playerCameraMove == null)
            {
                playerCameraMove = FindAnyObjectByType<CameraMove>();
                if (playerCameraMove == null)
                {
                    Debug.LogWarning("[LineViewManager] CameraMove 스크립트를 찾을 수 없어 시점을 이동할 수 없습니다.");
                    return;
                }
            }

            if (lineIndex < 0 || lineIndex >= 16)
            {
                Debug.LogWarning($"[LineViewManager] 잘못된 라인 인덱스입니다: {lineIndex}");
                return;
            }

            Transform targetPoint = lineViewPoints[lineIndex];
            
            Vector3 targetPosition;
            float targetYaw;
            float targetTilt;

            if (targetPoint != null)
            {
                targetPosition = targetPoint.position;
                targetYaw = targetPoint.eulerAngles.y;
                targetTilt = targetPoint.eulerAngles.x;
            }
            else
            {
                // 타겟 포인트가 에디터에 할당되지 않은 경우, 4x4 그리드 형태에 맞춰 임시 위치로 자동 계산
                // 현재 lineIndex 구조: A1=0, A2=1... B1=4... (행렬 스왑 전 유저가 할당한 Element 순서 유지)
                int letterIndex = lineIndex / 4; // 0~3 (A~D)
                int numberIndex = lineIndex % 4; // 0~3 (1~4)
                
                float spacingX = 15f; // 가로 간격 (A~D)
                float spacingZ = -15f; // 세로 간격 (1~4, 씬 방향에 따라 양수/음수 수정 가능)
                
                targetPosition = new Vector3(letterIndex * spacingX, 1.5f, numberIndex * spacingZ);
                targetYaw = 0f;
                targetTilt = 15f;
                
                string label = $"{(char)('A' + letterIndex)}{numberIndex + 1}";
                Debug.Log($"[LineViewManager] {label} 공정의 ViewPoint Transform이 할당되지 않아 임의의 4x4 위치로 이동합니다.");
            }

            // CameraMove 쪽에 새로 추가한 기능 호출하여 부드럽게 순간이동
            playerCameraMove.TeleportTo(targetPosition, targetYaw, targetTilt);
        }
    }
}
