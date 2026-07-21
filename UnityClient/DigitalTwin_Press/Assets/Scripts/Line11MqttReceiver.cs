using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace DigitalTwin.Line11
{
    /// <summary>
    /// Lightweight MQTT 3.1.1 subscriber for Solver input/result state messages.
    /// It intentionally has no external MQTT DLL dependency and applies Unity data
    /// only from Update(), keeping all Unity API access on the main thread.
    /// </summary>
    [DefaultExecutionOrder(-100)]
    public sealed class Line11MqttReceiver : MonoBehaviour
    {
        private const string DefaultBrokerAddress = "100.111.13.113";
        private const int DefaultBrokerPort = 1883;
        private const string DefaultTopic = "hydraulic-press/unity/state";
        private const int KeepAliveSeconds = 30;

        private static Line11MqttReceiver instance;

        [SerializeField] private string brokerAddress = DefaultBrokerAddress;
        [SerializeField] private int brokerPort = DefaultBrokerPort;
        [SerializeField] private string topic = DefaultTopic;
        [SerializeField, Min(0.5f)] private float noDataTimeoutSeconds = 3f;

        private readonly ConcurrentQueue<string> receivedMessages = new ConcurrentQueue<string>();
        private readonly ConcurrentQueue<string> connectionLogs = new ConcurrentQueue<string>();
        private Thread workerThread;
        private TcpClient activeClient;
        private volatile bool stopping;
        private bool initializedLiveCounters;
        private bool showingNoDataState;
        private Line11PressDetailController noDataController;

        public bool IsConnected { get; private set; }
        public DateTime LastMessageTime { get; private set; }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void CreateReceiver()
        {
            if (instance != null)
                return;

            GameObject receiverObject = new GameObject("Line11 MQTT Receiver");
            instance = receiverObject.AddComponent<Line11MqttReceiver>();
            DontDestroyOnLoad(receiverObject);
        }

        private void Awake()
        {
            if (instance != null && instance != this)
            {
                Destroy(gameObject);
                return;
            }

            instance = this;
            DontDestroyOnLoad(gameObject);
        }

        private void Start()
        {
            workerThread = new Thread(ConnectionLoop)
            {
                IsBackground = true,
                Name = "Line11 MQTT Receiver"
            };
            workerThread.Start();
        }

        private void Update()
        {
            while (connectionLogs.TryDequeue(out string log))
            {
                if (log.StartsWith("ERROR:", StringComparison.Ordinal))
                    Debug.LogWarning("[Line11 MQTT] " + log.Substring(6));
                else
                    Debug.Log("[Line11 MQTT] " + log);
            }

            int processed = 0;
            while (processed < 50 && receivedMessages.TryDequeue(out string json))
            {
                processed++;
                ApplySolverState(json);
            }

            ApplyNoDataStateWhenRequired();
        }

        private void OnDestroy()
        {
            if (instance == this)
                instance = null;

            stopping = true;
            try
            {
                activeClient?.Close();
            }
            catch (SocketException)
            {
                // The worker is already shutting down.
            }

            if (workerThread != null && workerThread.IsAlive)
                workerThread.Join(1000);
        }

        private void ConnectionLoop()
        {
            while (!stopping)
            {
                try
                {
                    RunConnectedSession();
                }
                catch (Exception exception) when (!stopping)
                {
                    IsConnected = false;
                    connectionLogs.Enqueue("ERROR:연결 끊김 - " + exception.Message + " (3초 후 재연결)");
                    WaitBeforeReconnect(3000);
                }
            }
        }

        private void RunConnectedSession()
        {
            TcpClient client = new TcpClient
            {
                NoDelay = true,
                ReceiveTimeout = 5000,
                SendTimeout = 5000
            };
            activeClient = client;

            try
            {
                IAsyncResult connect = client.BeginConnect(brokerAddress, brokerPort, null, null);
                if (!connect.AsyncWaitHandle.WaitOne(5000))
                    throw new TimeoutException("Broker 연결 시간 초과");
                client.EndConnect(connect);

                using (NetworkStream stream = client.GetStream())
                {
                    SendConnect(stream);
                    ValidateConnAck(ReadPacket(stream));
                    SendSubscribe(stream, 1, topic);
                    ValidateSubAck(ReadPacket(stream), 1);

                    IsConnected = true;
                    connectionLogs.Enqueue($"연결 완료: {brokerAddress}:{brokerPort}, topic={topic}");
                    DateTime lastNetworkWrite = DateTime.UtcNow;

                    while (!stopping && client.Connected)
                    {
                        if (stream.DataAvailable)
                        {
                            MqttPacket packet = ReadPacket(stream);
                            HandlePacket(stream, packet);
                        }
                        else
                        {
                            if ((DateTime.UtcNow - lastNetworkWrite).TotalSeconds >= KeepAliveSeconds * 0.6)
                            {
                                WritePacket(stream, new byte[] { 0xC0, 0x00 });
                                lastNetworkWrite = DateTime.UtcNow;
                            }

                            Thread.Sleep(20);
                        }
                    }
                }
            }
            finally
            {
                IsConnected = false;
                client.Close();
                if (activeClient == client)
                    activeClient = null;
            }
        }

        private void HandlePacket(NetworkStream stream, MqttPacket packet)
        {
            int packetType = packet.Header >> 4;
            if (packetType != 3)
                return;

            byte[] body = packet.Body;
            if (body.Length < 2)
                throw new InvalidDataException("잘못된 MQTT PUBLISH 패킷");

            int topicLength = (body[0] << 8) | body[1];
            int index = 2;
            if (topicLength <= 0 || index + topicLength > body.Length)
                throw new InvalidDataException("잘못된 MQTT topic 길이");

            string receivedTopic = Encoding.UTF8.GetString(body, index, topicLength);
            index += topicLength;

            int qos = (packet.Header >> 1) & 0x03;
            ushort packetId = 0;
            if (qos > 0)
            {
                if (index + 2 > body.Length)
                    throw new InvalidDataException("MQTT packet id가 없습니다");
                packetId = (ushort)((body[index] << 8) | body[index + 1]);
                index += 2;
            }

            if (receivedTopic == topic)
            {
                string payload = Encoding.UTF8.GetString(body, index, body.Length - index);
                receivedMessages.Enqueue(payload);
            }

            if (qos == 1)
            {
                WritePacket(stream, new byte[]
                {
                    0x40,
                    0x02,
                    (byte)(packetId >> 8),
                    (byte)(packetId & 0xFF)
                });
            }
        }

        private void ApplySolverState(string json)
        {
            try
            {
                JObject message = JObject.Parse(json);
                if (!string.Equals(
                        ReadString(message, "schema"),
                        "hydraulic-press.unity.v1",
                        StringComparison.Ordinal))
                {
                    return;
                }

                JObject input = message["input"] as JObject;
                JObject result = message["result"] as JObject;
                if (input == null || result == null)
                    throw new InvalidDataException("input 또는 result 객체가 없습니다");

                Line11PressDetailController controller = Line11PressDetailController.Instance;
                if (controller == null)
                    return;

                if (!initializedLiveCounters)
                {
                    initializedLiveCounters = true;
                    controller.SetWarningAlarmCount(0);
                    controller.SetReliefValveOpenCount(0);
                }

                float flow = ReadFloat(result, "calculated_flow_rate_L_min");
                float suctionVelocity = ReadFloat(result, "C_SUCTION_velocity_m_s");
                float pressureVelocity = ReadFloat(result, "C_PRESSURE_velocity_m_s");
                float aLineVelocity = ReadFloat(result, "C_A_LINE_velocity_m_s");
                float bLineVelocity = ReadFloat(result, "C_B_LINE_velocity_m_s");
                float returnVelocity = ReadFloat(result, "C_RETURN_velocity_m_s");
                float valvePaVelocity = ReadFloat(result, "V_DIR_PA_velocity_m_s");
                float valveBtVelocity = ReadFloat(result, "V_DIR_BT_velocity_m_s");
                float valvePbVelocity = ReadFloat(result, "V_DIR_PB_velocity_m_s");
                float valveAtVelocity = ReadFloat(result, "V_DIR_AT_velocity_m_s");
                float reliefVelocity = Mathf.Max(
                    ReadFloat(result, "V_RELIEF_velocity_m_s"),
                    ReadFloat(result, "C_RELIEF_velocity_m_s"));

                float pumpInPressure = ReadFloat(result, "N_PUMP_IN_pressure_bar_g");
                float pumpOutPressure = ReadFloat(result, "N_PUMP_OUT_pressure_bar_g");
                float valvePressure = ReadFloat(result, "N_VALVE_P_pressure_bar_g");
                float valveAPressure = ReadFloat(result, "N_VALVE_A_pressure_bar_g");
                float valveBPressure = ReadFloat(result, "N_VALVE_B_pressure_bar_g");
                float tankPressure = ReadFloat(result, "N_VALVE_T_pressure_bar_g");
                float loadPressure = ReadFloat(result, "load_pressure_bar_g");
                bool reliefOpen = ReadBool(result, "relief_open");

                controller.SetMetric(
                    "pipe-pump-tank",
                    flow,
                    suctionVelocity,
                    pumpInPressure);
                controller.SetMetric(
                    "pipe-pump-valve",
                    flow,
                    pressureVelocity,
                    valvePressure);
                controller.SetMetric(
                    "pipe-valve-tank-return",
                    flow,
                    returnVelocity,
                    tankPressure);
                controller.SetMetric(
                    "pipe-valve-tank-relief",
                    reliefOpen ? flow : 0f,
                    reliefVelocity,
                    valvePressure);
                controller.SetMetric(
                    "motor",
                    flow,
                    pressureVelocity,
                    pumpOutPressure);
                controller.SetMetric(
                    "valve-pa",
                    ActivePathFlow(flow, valvePaVelocity),
                    valvePaVelocity,
                    valvePressure);
                controller.SetMetric(
                    "valve-bt",
                    ActivePathFlow(flow, valveBtVelocity),
                    valveBtVelocity,
                    valveBPressure);
                controller.SetMetric(
                    "valve-pb",
                    ActivePathFlow(flow, valvePbVelocity),
                    valvePbVelocity,
                    valvePressure);
                controller.SetMetric(
                    "valve-at",
                    ActivePathFlow(flow, valveAtVelocity),
                    valveAtVelocity,
                    valveAPressure);
                controller.SetMetric(
                    "relief-valve",
                    reliefOpen ? flow : 0f,
                    reliefVelocity,
                    valvePressure);
                controller.SetMetric(
                    "cylinder",
                    flow,
                    Mathf.Max(aLineVelocity, bLineVelocity),
                    loadPressure);

                float temperature = ReadFloat(input, "Fluid.temperature_c", 0f);
                float pumpRpm = ReadFloat(
                    result,
                    "pump_rpm",
                    ReadFloat(input, "Cell.PUMP_01.rpm", 0f));
                float targetPressure = ReadFloat(
                    result,
                    "Press_target_pressure_bar_g",
                    ReadFloat(input, "Press.target_pressure_bar_g", 0f));
                float reliefSetPressure = ReadFloat(
                    input,
                    "Cell.V_RELIEF.set_pressure_bar_g",
                    0f);

                controller.SetOperatingMeasurements(temperature, pumpRpm);
                controller.SetPressureTargets(targetPressure, reliefSetPressure);
                controller.SetReliefValveOpen(reliefOpen);

                bool criticalSolverState = ReadBool(result, "contains_inf_or_nan") ||
                    !string.Equals(ReadString(result, "status"), "ok", StringComparison.OrdinalIgnoreCase);
                bool solverWarning = ReadBool(result, "solver_warning") || criticalSolverState;
                controller.SetSolverWarning(solverWarning);
                SetLine11FactoryStatus(
                    criticalSolverState
                        ? DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Critical
                        : solverWarning
                            ? DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Caution
                            : DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Normal);

                controller.SetLiveDataAvailable(true);
                LastMessageTime = DateTime.Now;
                showingNoDataState = false;
                noDataController = null;
            }
            catch (Exception exception)
            {
                Debug.LogWarning("[Line11 MQTT] 메시지 처리 실패: " + exception.Message);
            }
        }

        private void ApplyNoDataStateWhenRequired()
        {
            Line11PressDetailController controller = Line11PressDetailController.Instance;
            if (controller == null)
                return;

            bool hasNeverReceivedData = LastMessageTime == default;
            bool dataIsStale = !hasNeverReceivedData &&
                (DateTime.Now - LastMessageTime).TotalSeconds >= noDataTimeoutSeconds;
            if (!hasNeverReceivedData && !dataIsStale)
                return;

            if (showingNoDataState && noDataController == controller)
                return;

            controller.SetAllLiveValuesToZero();
            SetLine11FactoryStatus(DigitalTwin.View.FactoryTopViewCamera.FactoryStatus.Caution);
            showingNoDataState = true;
            noDataController = controller;
        }

        private static void SetLine11FactoryStatus(
            DigitalTwin.View.FactoryTopViewCamera.FactoryStatus status)
        {
            DigitalTwin.View.FactoryTopViewCamera topView =
                FindAnyObjectByType<DigitalTwin.View.FactoryTopViewCamera>();
            if (topView != null)
                topView.SetLineStatus(11, status);
        }

        private static float ActivePathFlow(float systemFlow, float pathVelocity)
        {
            return Mathf.Abs(pathVelocity) > 0.0001f ? systemFlow : 0f;
        }

        private static float ReadFloat(JObject source, string propertyName, float fallback = 0f)
        {
            JToken token = source[propertyName];
            if (token == null || token.Type == JTokenType.Null)
                return fallback;

            if (token.Type == JTokenType.Float || token.Type == JTokenType.Integer)
                return token.Value<float>();

            return float.TryParse(
                token.ToString(),
                NumberStyles.Float,
                CultureInfo.InvariantCulture,
                out float value)
                ? value
                : fallback;
        }

        private static bool ReadBool(JObject source, string propertyName)
        {
            JToken token = source[propertyName];
            if (token == null || token.Type == JTokenType.Null)
                return false;
            if (token.Type == JTokenType.Boolean)
                return token.Value<bool>();

            string value = token.ToString().Trim();
            return value.Equals("true", StringComparison.OrdinalIgnoreCase) ||
                value.Equals("yes", StringComparison.OrdinalIgnoreCase) ||
                value.Equals("open", StringComparison.OrdinalIgnoreCase) ||
                value == "1";
        }

        private static string ReadString(JObject source, string propertyName)
        {
            JToken token = source[propertyName];
            return token == null || token.Type == JTokenType.Null ? string.Empty : token.ToString();
        }

        private static void SendConnect(NetworkStream stream)
        {
            string clientId = "unity-line11-" + Environment.MachineName + "-" +
                Guid.NewGuid().ToString("N").Substring(0, 8);
            List<byte> body = new List<byte>();
            AddMqttString(body, "MQTT");
            body.Add(0x04); // MQTT 3.1.1
            body.Add(0x02); // Clean session
            body.Add((byte)(KeepAliveSeconds >> 8));
            body.Add((byte)(KeepAliveSeconds & 0xFF));
            AddMqttString(body, clientId);
            WritePacket(stream, BuildPacket(0x10, body));
        }

        private static void SendSubscribe(NetworkStream stream, ushort packetId, string subscribeTopic)
        {
            List<byte> body = new List<byte>
            {
                (byte)(packetId >> 8),
                (byte)(packetId & 0xFF)
            };
            AddMqttString(body, subscribeTopic);
            body.Add(0x01); // Requested QoS 1
            WritePacket(stream, BuildPacket(0x82, body));
        }

        private static void ValidateConnAck(MqttPacket packet)
        {
            if ((packet.Header >> 4) != 2 || packet.Body.Length != 2 || packet.Body[1] != 0)
                throw new IOException("Broker가 MQTT 연결을 거부했습니다");
        }

        private static void ValidateSubAck(MqttPacket packet, ushort expectedPacketId)
        {
            if ((packet.Header >> 4) != 9 || packet.Body.Length < 3)
                throw new IOException("MQTT SUBACK 응답이 올바르지 않습니다");

            ushort packetId = (ushort)((packet.Body[0] << 8) | packet.Body[1]);
            if (packetId != expectedPacketId || packet.Body[2] == 0x80)
                throw new IOException("Broker가 Unity topic 구독을 거부했습니다");
        }

        private static MqttPacket ReadPacket(NetworkStream stream)
        {
            int header = stream.ReadByte();
            if (header < 0)
                throw new EndOfStreamException("MQTT 연결이 종료됐습니다");

            int multiplier = 1;
            int remainingLength = 0;
            int encodedBytes = 0;
            int encoded;
            do
            {
                encoded = stream.ReadByte();
                if (encoded < 0)
                    throw new EndOfStreamException("MQTT remaining length가 없습니다");
                remainingLength += (encoded & 0x7F) * multiplier;
                multiplier *= 128;
                encodedBytes++;
                if (encodedBytes > 4)
                    throw new InvalidDataException("MQTT remaining length가 너무 큽니다");
            }
            while ((encoded & 0x80) != 0);

            byte[] body = new byte[remainingLength];
            int offset = 0;
            while (offset < remainingLength)
            {
                int read = stream.Read(body, offset, remainingLength - offset);
                if (read <= 0)
                    throw new EndOfStreamException("MQTT 패킷 수신 중 연결이 종료됐습니다");
                offset += read;
            }

            return new MqttPacket((byte)header, body);
        }

        private static byte[] BuildPacket(byte header, List<byte> body)
        {
            List<byte> packet = new List<byte>(body.Count + 5) { header };
            int remainingLength = body.Count;
            do
            {
                int encoded = remainingLength % 128;
                remainingLength /= 128;
                if (remainingLength > 0)
                    encoded |= 0x80;
                packet.Add((byte)encoded);
            }
            while (remainingLength > 0);

            packet.AddRange(body);
            return packet.ToArray();
        }

        private static void AddMqttString(List<byte> destination, string value)
        {
            byte[] encoded = Encoding.UTF8.GetBytes(value);
            if (encoded.Length > ushort.MaxValue)
                throw new ArgumentException("MQTT 문자열이 너무 깁니다", nameof(value));

            destination.Add((byte)(encoded.Length >> 8));
            destination.Add((byte)(encoded.Length & 0xFF));
            destination.AddRange(encoded);
        }

        private static void WritePacket(NetworkStream stream, byte[] packet)
        {
            stream.Write(packet, 0, packet.Length);
            stream.Flush();
        }

        private void WaitBeforeReconnect(int milliseconds)
        {
            int waited = 0;
            while (!stopping && waited < milliseconds)
            {
                Thread.Sleep(100);
                waited += 100;
            }
        }

        private readonly struct MqttPacket
        {
            public readonly byte Header;
            public readonly byte[] Body;

            public MqttPacket(byte header, byte[] body)
            {
                Header = header;
                Body = body;
            }
        }
    }
}
