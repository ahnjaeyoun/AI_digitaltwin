using UnityEngine;

namespace DigitalTwin.Press
{
    /// <summary>
    /// 유압 프레스의 샤프트(기둥)를 위아래로 실제 기계처럼 움직이게 하는 애니메이터 스크립트입니다.
    /// </summary>
    public class PressShaftAnimator : MonoBehaviour
    {
        [Tooltip("샤프트가 아래로 내려가는 최대 거리입니다. (기본값: 0.42)")]
        public float strokeDistance = 0.3f;
        
        [Tooltip("한 사이클(내려갔다 올라오기까지)에 걸리는 총 시간(초)입니다.")]
        public float cycleDuration = 15f;

        private Vector3 retractedPosition;
        private Vector3 pressedPosition;
        private float timeOffset;

        private void Start()
        {
            // 게임 시작 시점의 위치를 '올라간 상태(대기 상태)'로 저장합니다.
            retractedPosition = transform.localPosition;
            
            // 아래로 지정한 거리(strokeDistance)만큼 내려간 위치를 계산합니다.
            pressedPosition = retractedPosition + Vector3.down * strokeDistance;

            // 각 기계들이 똑같이 움직이면 어색하므로, 약간 엇박자로 움직이게 시작 타이밍(오프셋)을 랜덤하게 부여합니다.
            timeOffset = Random.Range(0f, cycleDuration);
        }

        private void LateUpdate()
        {
            // 시간에 따른 사이클 진행도 (0.0 ~ 1.0 반복)
            float cyclePhase = Mathf.Repeat((Time.time + timeOffset) / cycleDuration, 1f);
            float strokeNormalized = 0f;

            // 실제 유압 프레스의 4단계 사이클을 모방 (대기 -> 누름 -> 유지 -> 복귀)
            if (cyclePhase < 0.15f)
            {
                // 1. Loading (물건 싣기 대기 - 맨 위)
                strokeNormalized = 0f;
            }
            else if (cyclePhase < 0.55f)
            {
                // 2. Pressing (천천히 꾹 누르며 내려감)
                strokeNormalized = Mathf.InverseLerp(0.15f, 0.55f, cyclePhase);
            }
            else if (cyclePhase < 0.70f)
            {
                // 3. Holding (다 누른 상태로 꽉 쥐고 유지)
                strokeNormalized = 1f;
            }
            else
            {
                // 4. Returning (다시 위로 빠르게 올라옴)
                strokeNormalized = 1f - Mathf.InverseLerp(0.70f, 1f, cyclePhase);
            }

            // 뚝뚝 끊기지 않도록 부드러운 감속/가속(SmoothStep) 곡선 적용
            float easedStroke = Mathf.SmoothStep(0f, 1f, strokeNormalized);
            
            // 현재 프레임의 최종 위치 적용
            transform.localPosition = Vector3.Lerp(retractedPosition, pressedPosition, easedStroke);
        }
    }
}
