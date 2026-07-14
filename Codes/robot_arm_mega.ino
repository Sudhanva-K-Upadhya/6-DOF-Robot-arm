// Robot Arm Controller - Arduino Mega
// Protocol:
//   J<joint>,<steps>,<speed_us>\n  → move single joint, reply OK J<joint> ...
//   M<s0>,<s1>,<s2>,<s3>,<s4>,<s5>,<speed_us>\n → move ALL joints simultaneously, reply OK M ...
// steps can be negative (reverse direction)
// speed_us = microsecond delay between pulses (lower = faster)

#define NUM_JOINTS 6

volatile bool stopFlags[NUM_JOINTS] = {false};
volatile bool stopAll = false;

// {DIR_pin, PUL_pin}
const int PINS[NUM_JOINTS][2] = {
  {3,  2},   // J0 - Base
  {5,  4},   // J1 - Shoulder
  {7,  6},   // J2 - Elbow
  {9,  8},   // J3 - Wrist 1 (J4)
  {11, 10},  // J4 - Wrist 2 (J5)
  {13, 12},  // J5 - Wrist 3 (J6)
};

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < NUM_JOINTS; i++) {
    pinMode(PINS[i][0], OUTPUT);  // DIR
    pinMode(PINS[i][1], OUTPUT);  // PUL
    digitalWrite(PINS[i][0], HIGH);
    digitalWrite(PINS[i][1], HIGH);
  }
  Serial.println("READY");
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    while (Serial.available()) Serial.read();  // flush buffer

    if (line.length() == 0) return;

    char cmd = line.charAt(0);
    if (cmd == 'J') {
      // Clear stop flag for this joint before moving
      int joint = line.substring(1, line.indexOf(',')).toInt();
      if (joint >= 0 && joint < NUM_JOINTS) stopFlags[joint] = false;
      stopAll = false;
      parseSingleMove(line);
    } else if (cmd == 'M') {
      stopAll = false;
      for (int i = 0; i < NUM_JOINTS; i++) stopFlags[i] = false;
      parseMultiMove(line);
    } else if (cmd == 'S') {
      // Stop single joint
      int joint = line.substring(1).toInt();
      if (joint >= 0 && joint < NUM_JOINTS) {
        stopFlags[joint] = true;
        Serial.print("STOP J");
        Serial.println(joint);
      }
    } else if (cmd == 'X') {
      // Universal stop
      stopAll = true;
      for (int i = 0; i < NUM_JOINTS; i++) stopFlags[i] = true;
      Serial.println("STOP ALL");
    }
  }
}

// ── J command ─────────────────────────────────────────────────────────────────
void parseSingleMove(String line) {
  int c1 = line.indexOf(',');
  if (c1 < 0) return;
  int c2 = line.indexOf(',', c1 + 1);
  if (c2 < 0) return;

  int  joint    = line.substring(1, c1).toInt();
  long steps    = line.substring(c1 + 1, c2).toInt();
  unsigned int speed_us = (unsigned int)line.substring(c2 + 1).toInt();

  if (joint < 0 || joint >= NUM_JOINTS) return;
  if (speed_us < 100)  speed_us = 100;
  if (speed_us > 5000) speed_us = 5000;

  moveJoint(joint, steps, speed_us);

  Serial.print("OK J");
  Serial.print(joint);
  Serial.print(" steps=");
  Serial.print(steps);
  Serial.print(" spd=");
  Serial.println(speed_us);
}

// ── M command ─────────────────────────────────────────────────────────────────
// Format: M<s0>,<s1>,<s2>,<s3>,<s4>,<s5>,<speed_us>
// All 6 step values + 1 speed = 7 comma-separated values after 'M'
void parseMultiMove(String line) {
  // Strip leading 'M'
  String data = line.substring(1);

  long steps[NUM_JOINTS];
  unsigned int speed_us = 800;

  // Parse 7 comma-separated tokens: 6 steps + 1 speed
  int prev = 0;
  int tokenIdx = 0;
  for (int i = 0; i <= data.length() && tokenIdx <= NUM_JOINTS; i++) {
    if (i == (int)data.length() || data.charAt(i) == ',') {
      String token = data.substring(prev, i);
      if (tokenIdx < NUM_JOINTS) {
        steps[tokenIdx] = token.toInt();
      } else {
        // Last token = speed
        speed_us = (unsigned int)token.toInt();
      }
      tokenIdx++;
      prev = i + 1;
    }
  }

  if (tokenIdx < NUM_JOINTS + 1) return;  // incomplete command

  if (speed_us < 100)  speed_us = 100;
  if (speed_us > 5000) speed_us = 5000;

  moveAllJoints(steps, speed_us);

  Serial.print("OK M steps=");
  for (int i = 0; i < NUM_JOINTS; i++) {
    Serial.print(steps[i]);
    if (i < NUM_JOINTS - 1) Serial.print(",");
  }
  Serial.print(" spd=");
  Serial.println(speed_us);
}

// ── Single joint move ─────────────────────────────────────────────────────────
void moveJoint(int joint, long steps, unsigned int speed_us) {
  int dirPin = PINS[joint][0];
  int pulPin = PINS[joint][1];

  if (steps == 0) return;

  digitalWrite(dirPin, steps > 0 ? HIGH : LOW);
  if (steps < 0) steps = -steps;

  delayMicroseconds(5);  // DIR settle time for TB6600

  for (long s = 0; s < steps; s++) {
    if (stopFlags[joint] || stopAll) {
      digitalWrite(pulPin, LOW);
      Serial.print("STOPPED J");
      Serial.println(joint);
      return;
    }
    digitalWrite(pulPin, HIGH);
    delayMicroseconds(speed_us);
    digitalWrite(pulPin, LOW);
    delayMicroseconds(speed_us);
  }

  digitalWrite(dirPin, LOW);
  digitalWrite(pulPin, LOW);
}

// ── Simultaneous multi-joint move (interleaved stepping) ─────────────────────
// All joints start together. Each joint gets a pulse every iteration
// only when it still has steps remaining. Joints that finish early
// simply stop pulsing while others continue.
void moveAllJoints(long steps[], unsigned int speed_us) {
  // Set directions and compute absolute step counts
  long remaining[NUM_JOINTS];
  for (int i = 0; i < NUM_JOINTS; i++) {
    if (steps[i] == 0) {
      remaining[i] = 0;
      continue;
    }
    digitalWrite(PINS[i][0], steps[i] > 0 ? HIGH : LOW);
    remaining[i] = steps[i] < 0 ? -steps[i] : steps[i];
  }

  delayMicroseconds(5);  // DIR settle for all TB6600 drivers

  // Interleaved stepping loop
  bool anyLeft = true;
  while (anyLeft) {
    anyLeft = false;
    if (stopAll) {
      for (int i = 0; i < NUM_JOINTS; i++) {
        digitalWrite(PINS[i][1], LOW);
      }
      Serial.println("STOPPED ALL");
      return;
    }
    for (int i = 0; i < NUM_JOINTS; i++) {
      if (remaining[i] > 0 && !stopFlags[i]) {
        digitalWrite(PINS[i][1], HIGH);  // PUL HIGH
      }
    }
    delayMicroseconds(speed_us);

    for (int i = 0; i < NUM_JOINTS; i++) {
      if (remaining[i] > 0 && !stopFlags[i]) {
        digitalWrite(PINS[i][1], LOW);   // PUL LOW
        remaining[i]--;
        if (remaining[i] > 0) anyLeft = true;
      }
    }
    delayMicroseconds(speed_us);
  }

  // Return all pins to LOW
  for (int i = 0; i < NUM_JOINTS; i++) {
    digitalWrite(PINS[i][0], LOW);
    digitalWrite(PINS[i][1], LOW);
  }
}
