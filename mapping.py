"""Strava activity -> Walktober exercise activity.

Walktober converts exercise to steps as minutes x steps_per_minute (ids are from
GET /promotions/<id>/exercise_activities). Each rule returns (exercise_id, why) or
(None, why) to skip. Walking-type activities are skipped on purpose: their steps are
already in the daily step count from Fitbit/Google Health, so logging them again as
exercise would double-count.
"""
import re

# Walktober exercise ids (from the challenge catalogue; check yours with probe.py), with steps/min for reference.
BIKE_LIGHT, BIKE_MOD, BIKE_FAST = 144, 145, 146            # 181 / 213 / 266
STATIONARY_LIGHT, STATIONARY_MOD, STATIONARY_HARD = 147, 148, 149   # 181 / 319 / 420
MOUNTAIN_BIKE = 210                                          # 226
STRETCHING = 250                                             # 61
YOGA, POWER_YOGA = 271, 272                                  # 67 / 106
PILATES, BARRE = 214, 187                                    # 80 / 133
CALISTHENICS = 154                                           # 101
STRENGTH_MOD, STRENGTH_VIG = 248, 249                        # 93 / 160
BOOT_CAMP, HIGH_IMPACT = 188, 189                            # 261 / 194
SURFING, WATER_SKIING, SUP = 251, 266, 247                   # 80 / 160 / 160

# Strava sport_type -> exercise id, for types with one sensible match.
BY_SPORT = {
    "StandUpPaddling": SUP, "Kayaking": 205, "Canoeing": 156, "Rowing": 221,
    "VirtualRow": 219, "Swim": 252, "Elliptical": 175, "StairStepper": 246,
    "AlpineSki": 172, "BackcountrySki": 161, "NordicSki": 161, "Snowboard": 239,
    "Snowshoe": 141, "IceSkate": 231, "InlineSkate": 232, "Skateboard": 230,
    "Golf": 185, "Pickleball": 213, "Tennis": 256, "Badminton": 140, "Squash": 244,
    "Racquetball": 218, "TableTennis": 215, "Soccer": 242, "RockClimbing": 159,
    "Crossfit": 163, "HighIntensityIntervalTraining": BOOT_CAMP, "Pilates": PILATES,
    "Kitesurf": WATER_SKIING, "Windsurf": WATER_SKIING, "Sail": 228,
}
# Counted by the wrist step counter already -> skip.
STEP_COUNTED = {"Walk", "Hike", "Run", "TrailRun", "VirtualRun"}
# Peloton class titles -> exercise id. First match wins, so order matters.
WORKOUT_KEYWORDS = [
    (r"meditat|breath|sleep", None),
    (r"stretch|mobility|foam roll|recovery", STRETCHING),
    (r"power yoga|yoga flow", POWER_YOGA),
    (r"yoga", YOGA),
    (r"pilates", PILATES),
    (r"barre", BARRE),
    (r"boot ?camp|hiit|tabata", BOOT_CAMP),
    (r"cardio|dance|shadowbox", HIGH_IMPACT),
    (r"core|abs\b|plank", CALISTHENICS),
    (r"arms|weights|strength|upper body|lower body|glutes|legs|full body|kettlebell", STRENGTH_MOD),
]


def _mph(a):
    return (a.get("average_speed") or 0) * 2.23694


def map_activity(a):
    """a: dict with sport_type, name, trainer, average_watts, average_heartrate, average_speed."""
    sport, name = a.get("sport_type") or a.get("type") or "", (a.get("name") or "").lower()
    hr, watts = a.get("average_heartrate") or 0, a.get("average_watts") or 0

    if sport in STEP_COUNTED:
        return None, "steps already counted by Fitbit"

    if sport in ("Ride", "VirtualRide", "EBikeRide", "MountainBikeRide", "GravelRide"):
        if sport == "MountainBikeRide":
            return MOUNTAIN_BIKE, "mountain bike"
        if sport == "VirtualRide" or a.get("trainer"):
            # Indoor: Peloton "speed" is synthetic, so grade by power, else heart rate.
            if watts:
                eid = STATIONARY_HARD if watts > 180 else STATIONARY_MOD if watts >= 100 else STATIONARY_LIGHT
                return eid, f"indoor, {watts:.0f} W avg"
            if hr:
                eid = STATIONARY_HARD if hr >= 135 else STATIONARY_MOD if hr >= 110 else STATIONARY_LIGHT
                return eid, f"indoor, no power, HR {hr:.0f}"
            return STATIONARY_MOD, "indoor, no power or HR"
        mph = _mph(a)
        eid = BIKE_FAST if mph >= 15 else BIKE_MOD if mph >= 12 else BIKE_LIGHT
        return eid, f"outdoor, {mph:.1f} mph avg"

    if sport == "Surfing":
        if re.search(r"wakeboard|water ?ski|kite|wing", name):
            return WATER_SKIING, "tow/wind sport titled as surfing"
        return SURFING, "surfing"

    if sport in ("WeightTraining",):
        return (STRENGTH_VIG if hr >= 130 else STRENGTH_MOD), f"weights, HR {hr:.0f}"
    if sport == "Yoga":
        return (POWER_YOGA if "power" in name else YOGA), "yoga"

    if sport in BY_SPORT:
        return BY_SPORT[sport], sport

    if sport == "Workout":
        for pat, eid in WORKOUT_KEYWORDS:
            if re.search(pat, name):
                return eid, f"title matches /{pat}/" if eid else "not exercise"
        # Untitled watch workouts ("Evening Workout"): grade by heart rate.
        return (STRENGTH_VIG if hr >= 130 else STRENGTH_MOD), f"generic workout, HR {hr:.0f}"

    return None, f"no mapping for {sport}"
