using UnityEngine;





namespace UnityFactorySceneHDRP
{
	public class CameraMove : MonoBehaviour
	{
		[SerializeField] private CharacterController _characterController;
		[SerializeField] private Transform _playerRoot;
		[SerializeField] private Transform _camera;

		[Space(10)]
		[SerializeField] private float _moveSpeed = 2;
		[SerializeField] private float _rotateSpeed = 2;

		[Space(10)]
		[SerializeField] private float _minWorldY;


		private float _yaw = 0;
		private float _tilt = 0;
		private bool _isRunning = false;
		private bool _isWalkMode = true;
		
		private float _verticalVelocity = 0f;
		private float _gravity = -9.81f;
		[SerializeField] private float _jumpForce = 5f;

		// 외부 시점 이동 제어를 위한 변수들
		private bool _isTransitioning = false;
		private Vector3 _targetPosition;
		private float _targetYaw;
		private float _targetTilt;
		private float _transitionProgress = 0f;
		private float _transitionSpeed = 1.5f; // 부드러운 이동 속도
		private Vector3 _startPosition;
		private float _startYaw;
		private float _startTilt;




		private void Awake()
		{
			_yaw = _playerRoot.eulerAngles.y;
			_tilt = _camera.localEulerAngles.x;
		}

		/// <summary>
		/// 외부에서 목표 위치와 시야각을 지정하여 플레이어를 이동시킵니다.
		/// </summary>
		public void TeleportTo(Vector3 targetPosition, float targetYaw, float targetTilt)
		{
			_targetPosition = targetPosition;
			_targetYaw = targetYaw;
			_targetTilt = targetTilt;

			_startPosition = _playerRoot.position;
			_startYaw = _yaw;
			_startTilt = _tilt;

			_transitionProgress = 0f;
			_isTransitioning = true;

			// 설정한 뷰포인트가 공중에 있을 때 중력에 의해 떨어지지 않도록 비행 모드(Fly Mode)로 강제 전환
			_isWalkMode = false;

			// 이동 중에는 충돌체 간섭을 방지하기 위해 CharacterController 비활성화
			if (_characterController != null)
				_characterController.enabled = false;
		}



		private void Update()
		{
			// 자동 이동 연출 중일 때는 플레이어 입력 무시
			if (_isTransitioning)
			{
				_transitionProgress += Time.deltaTime * _transitionSpeed;
				if (_transitionProgress >= 1f)
				{
					_transitionProgress = 1f;
					_isTransitioning = false;

					if (_characterController != null)
						_characterController.enabled = true;
				}

				// SmoothStep을 활용하여 부드럽게 감속/가속 이동
				float t = Mathf.SmoothStep(0f, 1f, _transitionProgress);

				_playerRoot.position = Vector3.Lerp(_startPosition, _targetPosition, t);
				_yaw = Mathf.LerpAngle(_startYaw, _targetYaw, t);
				_tilt = Mathf.LerpAngle(_startTilt, _targetTilt, t);

				_playerRoot.eulerAngles = new Vector3(0, _yaw, 0);
				_camera.localEulerAngles = new Vector3(_tilt, 0, 0);

				// 모드에 따라 카메라 로컬 높이 고정
				if (_isWalkMode)
					_camera.localPosition = new Vector3(0, 1.5f, 0);
				else
					_camera.localPosition = Vector3.zero;

				return;
			}

			// Rotate
			if(Input.GetMouseButton(1))
			{
				_yaw  += Input.GetAxis("Mouse X") * _rotateSpeed;
				_tilt -= Input.GetAxis("Mouse Y") * _rotateSpeed;

				_tilt = Mathf.Clamp(_tilt, -89, 89);

				_playerRoot.eulerAngles = new Vector3(0, _yaw, 0);
				_camera.localEulerAngles = new Vector3(_tilt, 0, 0);
			}

			// Move
			Vector3 dir = new Vector3(Input.GetAxis("Horizontal"), 0 , Input.GetAxis("Vertical"));
			float height = Mathf.Max(0, _camera.localPosition.y + ((Input.GetKey(KeyCode.Q) ? -_moveSpeed : 0) + (Input.GetKey(KeyCode.E) ? _moveSpeed : 0)) * Time.deltaTime);

			if(Input.GetKeyDown(KeyCode.LeftShift) || Input.GetKeyDown(KeyCode.RightShift))
			{
				_isRunning = !_isRunning;
			}

			if(_isWalkMode)
			{
				dir = Quaternion.Euler(0, _playerRoot.localEulerAngles.y, 0) * dir;
				Vector3 moveDir = dir * _moveSpeed * (_isRunning ? 3 : 1);

				// 점프 및 중력 처리 로직
				if (_characterController.isGrounded)
				{
					_verticalVelocity = -2f; // 바닥에 안정적으로 붙어있기 위해 작은 음수 유지

					if (Input.GetKeyDown(KeyCode.Space))
					{
						_verticalVelocity = _jumpForce;
					}
				}

				_verticalVelocity += _gravity * Time.deltaTime;
				moveDir.y = _verticalVelocity;

				_characterController.Move(moveDir * Time.deltaTime);
				_camera.localPosition = new Vector3(0, height, 0);
			}
			else
			{
				dir = Quaternion.Euler(_camera.localEulerAngles.x, _playerRoot.localEulerAngles.y, _camera.localEulerAngles.z) * dir;
				_characterController.Move(dir * _moveSpeed * (_isRunning ? 3 : 1) * Time.deltaTime);
			}

			if(_playerRoot.position.y < _minWorldY)
			{
				Vector3 position = _playerRoot.position;
				position.y = _minWorldY;
				_playerRoot.position = position;
			}

			// Change mode
			if(Input.GetKeyDown(KeyCode.F))
			{
				_isWalkMode = !_isWalkMode;
				if(_isWalkMode)
				{
					_playerRoot.position = new Vector3(_playerRoot.position.x, _minWorldY, _playerRoot.position.z);
					_camera.localPosition = new Vector3(0, 1.5f, 0);
				}
				else
				{
					_playerRoot.position = new Vector3(_playerRoot.position.x, _camera.position.y, _playerRoot.position.z);
					_camera.localPosition = Vector3.zero;
				}
			}
		}
	}
}