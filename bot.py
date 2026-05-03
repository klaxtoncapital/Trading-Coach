import discord
from discord.ext import commands, tasks
import asyncio
import aiohttp
import json
import os
from datetime import datetime, time
import pytz
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
BRIEF_CHANNEL = os.getenv("BRIEF_CHANNEL", "morning-brief")
ALERTS_CHANNEL = os.getenv("ALERTS_CHANNEL", "alerts")
JOURNAL_CHANNEL = os.getenv("JOURNAL_CHANNEL", "trade-journal")
LONDON_TZ = pytz.timezone("Europe/London")
NY_TZ = pytz.timezone("America/New_York")

# ─────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", help_command=None, intents=intents)

# ─────────────────────────────────────────────
# WEEKLY PROFILE TRACKER
# Stored in memory — resets each Sunday
# ─────────────────────────────────────────────
weekly_state = {
    "monday":    {"range": None, "direction": None, "high": None, "low": None, "large_expansion": False},
    "tuesday":   {"range": None, "direction": None, "high": None, "low": None},
    "wednesday": {"range": None, "direction": None, "high": None, "low": None},
    "thursday":  {"range": None, "direction": None, "high": None, "low": None},
    "friday":    {"range": None, "direction": None, "high": None, "low": None},
    "profile":   None,  # classic_expansion / midweek_reversal / consolidation_reversal / thursday_counter
    "bias":      None,  # bullish / bearish
    "c2_confirmed": False,
    "active_alerts": [],
    "weekly_open": None,
    "pwh": None,
    "pwl": None,
    "pdh": None,
    "pdl": None,
    "pmh": None,
    "pml": None,
    "asian_high": None,
    "asian_low": None,
    "london_high": None,
    "london_low": None,
    "nypm_high": None,
    "nypm_low": None,
    "active_fvgs": [],
    "active_obs": [],
    "sequential_targets": []  # {level_name, level_price, target_name, target_price, active}
}

# ─────────────────────────────────────────────
# WEEKLY PROFILE LOGIC
# ─────────────────────────────────────────────
def determine_weekly_profile():
    """
    Analyses daily candle data and determines which of the 4 weekly 
    expansion profiles is forming or confirmed.

    4 PROFILES:
    1. Classic Expansion     — Monday small, Tuesday C2 sweeps Monday H/L + closes back inside
    2. Midweek Reversal      — Mon+Tue consolidated, Wednesday C2 sweeps + closes back inside
    3. Consolidation Reversal— Mon+Tue+Wed consolidated, Thursday C2 sweeps + closes back inside  
    4. Thursday Counter      — Mon+Tue+Wed all expand same direction, Thursday C2 counter sweep

    C2 = confirmation candle (sweep + close back inside)
    C3/C4 = expansion candles (entry targets)
    """
    mon = weekly_state["monday"]
    tue = weekly_state["tuesday"]
    wed = weekly_state["wednesday"]
    thu = weekly_state["thursday"]

    profile_status = {
        "profile": None,
        "status": "watching",  # watching / possible / confirmed / invalidated
        "c2_day": None,
        "expansion_days": [],
        "notes": []
    }

    # ── CLASSIC EXPANSION ──
    # Monday must be small/inside (not large expansion)
    if mon["direction"] is not None:
        if not mon["large_expansion"]:
            profile_status["profile"] = "classic_expansion"
            profile_status["c2_day"] = "tuesday"
            profile_status["expansion_days"] = ["wednesday", "thursday"]
            profile_status["status"] = "possible"
            profile_status["notes"].append(
                "Monday was small/inside range. Classic Expansion possible. "
                "Alerts set on MONDAY HIGH and MONDAY LOW. "
                "Tuesday must sweep Monday's high or low AND close back inside to confirm C2."
            )

            # Check if Tuesday confirmed C2
            if tue["direction"] is not None and weekly_state["c2_confirmed"]:
                profile_status["status"] = "confirmed"
                profile_status["notes"].append(
                    "Tuesday (C2) CONFIRMED. Classic Expansion validated. "
                    "Wednesday (C3) and Thursday (C4) are your expansion candles."
                )
        else:
            profile_status["notes"].append(
                "Monday had LARGE expansion. No high probability trade until Thursday or Friday. "
                "Watching for Thursday Counter or Consolidation Reversal."
            )

    # ── MIDWEEK REVERSAL ──
    # Monday AND Tuesday both consolidated/opposing
    if (mon["direction"] is not None and tue["direction"] is not None):
        mon_consolidated = not mon["large_expansion"]
        tue_consolidated = not tue.get("large_expansion", False)

        if mon_consolidated and tue_consolidated:
            profile_status["profile"] = "midweek_reversal"
            profile_status["c2_day"] = "wednesday"
            profile_status["expansion_days"] = ["thursday", "friday"]
            profile_status["status"] = "possible"
            profile_status["notes"].append(
                "Monday and Tuesday consolidated/opposing. Midweek Reversal possible. "
                "Alerts set on TUESDAY HIGH and TUESDAY LOW. "
                "Wednesday must sweep Tuesday's high or low AND close back inside to confirm C2."
            )

            if wed["direction"] is not None and weekly_state["c2_confirmed"]:
                profile_status["status"] = "confirmed"
                profile_status["notes"].append(
                    "Wednesday (C2) CONFIRMED. Midweek Reversal validated. "
                    "Thursday (C3) and Friday (C4) are your expansion candles."
                )

    # ── CONSOLIDATION REVERSAL ──
    # Monday, Tuesday AND Wednesday all consolidated
    if (mon["direction"] is not None and tue["direction"] is not None 
            and wed["direction"] is not None):
        all_consolidated = (
            not mon["large_expansion"] and
            not tue.get("large_expansion", False) and
            not wed.get("large_expansion", False)
        )

        if all_consolidated:
            profile_status["profile"] = "consolidation_reversal"
            profile_status["c2_day"] = "thursday"
            profile_status["expansion_days"] = ["friday"]
            profile_status["status"] = "possible"
            profile_status["notes"].append(
                "Three days of consolidation confirmed. Consolidation Reversal possible. "
                "Alerts set on WEDNESDAY HIGH and WEDNESDAY LOW. "
                "Thursday must sweep Wednesday's high or low AND close back inside to confirm C2."
            )

            if thu["direction"] is not None and weekly_state["c2_confirmed"]:
                profile_status["status"] = "confirmed"
                profile_status["notes"].append(
                    "Thursday (C2) CONFIRMED. Consolidation Reversal validated. "
                    "Friday (C3) is your expansion candle."
                )

    # ── THURSDAY COUNTER ──
    # Monday, Tuesday AND Wednesday all expanded in SAME direction
    if (mon["direction"] is not None and tue["direction"] is not None 
            and wed["direction"] is not None):
        same_direction = (
            mon["direction"] == tue["direction"] == wed["direction"]
        )
        all_expanded = (
            mon["large_expansion"] or True  # any expansion counts
        )

        if same_direction:
            direction = mon["direction"]
            counter_direction = "bullish" if direction == "bearish" else "bearish"
            profile_status["profile"] = "thursday_counter"
            profile_status["c2_day"] = "thursday"
            profile_status["expansion_days"] = ["friday"]
            profile_status["status"] = "possible"
            profile_status["notes"].append(
                f"THREE consecutive {direction} expansion days (Mon/Tue/Wed). "
                f"THURSDAY COUNTER in play. "
                f"Alert set on WEDNESDAY LOW (bullish counter) or WEDNESDAY HIGH (bearish counter). "
                f"Thursday must sweep Wednesday's {'low' if direction == 'bearish' else 'high'} "
                f"AND close back inside to confirm C2. "
                f"Friday (C3) expansion target: {counter_direction.upper()}."
            )

            if thu["direction"] is not None and weekly_state["c2_confirmed"]:
                profile_status["status"] = "confirmed"
                profile_status["notes"].append(
                    f"⚡ THURSDAY COUNTER (C2) CONFIRMED. "
                    f"Friday (C3) {counter_direction} expansion expected. "
                    f"CHECK SMT + H1 NOW."
                )

    return profile_status


def get_confluence_score():
    """
    Scores current setup out of 100 based on confluence factors.
    Each factor adds points to the total.
    """
    score = 0
    checklist = []

    # HTF Bias confirmed (20 pts)
    if weekly_state["bias"]:
        score += 20
        checklist.append(f"✅ HTF Bias confirmed: {weekly_state['bias'].upper()} (+20)")
    else:
        checklist.append("❌ HTF Bias: Not confirmed (0)")

    # Weekly profile identified (15 pts)
    profile = determine_weekly_profile()
    if profile["profile"] and profile["status"] in ["possible", "confirmed"]:
        score += 15
        checklist.append(f"✅ Weekly profile: {profile['profile'].replace('_', ' ').title()} (+15)")
    else:
        checklist.append("⚠️ Weekly profile: Not yet identified (0)")

    # C2 confirmed (20 pts)
    if weekly_state["c2_confirmed"]:
        score += 20
        checklist.append("✅ C2 candle confirmed — sweep + close back inside (+20)")
    else:
        checklist.append("⚠️ C2 candle: Not yet confirmed — watching for sweep + close (0)")

    # Key level identified (10 pts)
    if any([weekly_state["pdl"], weekly_state["pdh"], weekly_state["pwl"], weekly_state["pwh"]]):
        score += 10
        checklist.append("✅ Key levels identified: PDL/PDH/PWL/PWH (+10)")
    else:
        checklist.append("❌ Key levels: Not set (0)")

    # FVG present (10 pts)
    if weekly_state["active_fvgs"]:
        score += 10
        checklist.append(f"✅ Active FVG present (+10)")
    else:
        checklist.append("⚠️ FVG: None identified near price (0)")

    # SMT placeholder (15 pts) — manually updated
    checklist.append("⚠️ SMT vs ES/DOW: Monitor manually at key level (0)")

    # H1 alignment placeholder (10 pts)
    checklist.append("⚠️ H1 structure: Check after key level alert fires (0)")

    return score, checklist


def get_premium_discount_status():
    """
    Determines if price is in premium or discount relative to the active range.
    Uses PWH/PWL or PDH/PDL as the range boundaries.
    """
    high = weekly_state.get("pwh") or weekly_state.get("pdh")
    low = weekly_state.get("pwl") or weekly_state.get("pdl")

    if not high or not low:
        return "⚠️ Range not set — update PWH/PWL or PDH/PDL"

    midpoint = (high + low) / 2
    range_label = "Weekly Range" if weekly_state.get("pwh") else "Daily Range"

    return {
        "high": high,
        "low": low,
        "midpoint": midpoint,
        "range_label": range_label,
        "50_percent": midpoint
    }


# ─────────────────────────────────────────────
# NEWS EVENT RULES
# ─────────────────────────────────────────────
def get_news_rules(news_events):
    """
    Takes a list of today's news events and returns
    the correct trading rules and timing guidance.
    """
    rules = []
    times = [e["time"] for e in news_events]
    has_830 = any("08:30" in t for t in times)
    has_945 = any("09:45" in t for t in times)
    has_10am = any("10:00" in t for t in times)
    has_fomc = any("FOMC" in e.get("name", "") or "Federal" in e.get("name", "") 
                   for e in news_events)
    has_nfp = any("NFP" in e.get("name", "") or "Non-Farm" in e.get("name", "") 
                  for e in news_events)

    if not news_events:
        rules.append({
            "rule": "NO HIGH IMPACT NEWS",
            "guidance": "Focus on 09:30am NY as the manipulation. "
                       "This is your key time to watch for the sweep and entry."
        })

    if has_fomc:
        rules.append({
            "rule": "⚠️ FOMC DAY",
            "guidance": "Wait for the LAST FOMC event to pass. "
                       "Engage POST 2:45pm NY time only. "
                       "Usually runs BOTH sides of liquidity before the real move. "
                       "DO NOT get trapped on the first spike."
        })

    if has_nfp:
        rules.append({
            "rule": "⚠️ NFP DAY (Friday 08:30am)",
            "guidance": "NFP will either be manipulation OR distribution.\n"
                       "• If NFP is distribution IN DIRECTION of HTF draw → SIT OUT all day, it will be a mess.\n"
                       "• If NFP is manipulation AGAINST HTF draw → 09:30am and/or 10am can give a scalp only.\n"
                       "• 10am is usually cleaner on NFP days.\n"
                       "Rule: NFP Distribution → 09:30am mess."
        })

    if has_830 and has_945:
        rules.append({
            "rule": "⚠️ 08:30AM + 09:45AM TWO-STAGE EVENT",
            "guidance": "ALWAYS wait for 10:00am. "
                       "Market will chop in seek & destroy profile from 08:30 until 10:00am. "
                       "Real manipulation and move happens at 10am. "
                       "TIP: 10am is a new H4 candle — watch H4 candle closures closely."
        })
    elif has_830 and not has_945:
        rules.append({
            "rule": "08:30AM NEWS (Most Common)",
            "guidance": "Two scenarios:\n"
                       "• 08:30am = manipulation → 09:30am = distribution\n"
                       "• 08:30am moves AGAINST your liquidity, doesn't reach it → "
                       "09:30am runs it and reverses VERY quickly.\n"
                       "Watch which direction 08:30am moves relative to your key level."
        })

    if has_10am and not has_830:
        rules.append({
            "rule": "10:00AM NEWS ONLY",
            "guidance": "Always wait for 09:30am first.\n"
                       "• If 09:30am was manipulation → expect 10am distribution\n"
                       "• If Asia and London trending one direction with only 10am news → "
                       "10am will wipe out London/Asia price action entirely.\n"
                       "Rule: 09:30am manipulation → 10am distribution."
        })

    return rules


# ─────────────────────────────────────────────
# SEQUENTIAL TARGET TRACKER
# ─────────────────────────────────────────────
def add_sequential_target(level_name, level_price, target_name, target_price):
    """
    When a key level is hit, record it and its connected sequential target.
    This stays active until the target is reached.
    """
    weekly_state["sequential_targets"].append({
        "level_name": level_name,
        "level_price": level_price,
        "target_name": target_name,
        "target_price": target_price,
        "active": True,
        "timestamp": datetime.now(LONDON_TZ).strftime("%d/%m/%Y %H:%M")
    })


def get_sequential_target_for_level(level_name):
    """
    Returns the correct sequential target based on which level was hit.
    
    HIERARCHY:
    PSH/PSL → targets PDL/PDH
    PDH/PDL → targets PWL/PWH  
    PWH/PWL → targets PML/PMH
    """
    targets = {
        "PSL":  ("PDH", weekly_state.get("pdh")),
        "PSH":  ("PDL", weekly_state.get("pdl")),
        "PDL":  ("PWH", weekly_state.get("pwh")),
        "PDH":  ("PWL", weekly_state.get("pwl")),
        "PWL":  ("PMH", weekly_state.get("pmh")),
        "PWH":  ("PML", weekly_state.get("pml")),
        "ASL":  ("PDH", weekly_state.get("pdh")),
        "ASH":  ("PDL", weekly_state.get("pdl")),
        "NYPM-L": ("PDH", weekly_state.get("pdh")),
        "NYPM-H": ("PDL", weekly_state.get("pdl")),
    }
    return targets.get(level_name, ("Unknown", None))


# ─────────────────────────────────────────────
# DISCORD MESSAGE FORMATTERS
# ─────────────────────────────────────────────
def format_morning_brief():
    """
    Formats the complete daily morning brief message.
    Sent every morning at 6:30am London time.
    """
    london_now = datetime.now(LONDON_TZ)
    ny_now = datetime.now(NY_TZ)
    day_name = london_now.strftime("%A").upper()
    date_str = london_now.strftime("%d %B %Y")
    day_of_week = london_now.weekday()  # 0=Monday, 6=Sunday

    profile = determine_weekly_profile()
    score, checklist = get_confluence_score()
    pd_status = get_premium_discount_status()

    # Day-specific context
    day_context = {
        0: "MONDAY — Week begins. Watch for sweep of Friday's low or NY PM low. "
           "If Monday sweeps Friday's low → week has begun. "
           "Alert set on FRIDAY LOW and FRIDAY HIGH.",
        1: "TUESDAY — If Monday was small, Classic Expansion C2 watch. "
           "Alert active on MONDAY HIGH and MONDAY LOW. "
           "If Tuesday sweeps and closes back inside → C2 confirmed, drop to H1.",
        2: "WEDNESDAY — Midweek. If Mon+Tue consolidated → Midweek Reversal C2 watch. "
           "If Mon+Tue+Wed all same direction → Thursday Counter setting up.",
        3: "THURSDAY — Key day. Check if Thursday Counter or Consolidation Reversal is in play. "
           "This is ICT Thursday delivery day — highest probability reversal sessions.",
        4: "FRIDAY — Expansion or continuation day. "
           "If Classic Expansion confirmed → C4 continuation. "
           "If Thursday Counter confirmed → C3 expansion today. "
           "NFP if first Friday of month."
    }

    # Format sequential targets
    active_targets = [t for t in weekly_state["sequential_targets"] if t["active"]]
    targets_text = ""
    if active_targets:
        targets_text = "\n**🎯 ACTIVE SEQUENTIAL TARGETS:**\n"
        for t in active_targets:
            targets_text += (f"**{t['level_name']}** {t['level_price']} "
                           f"→→→ **{t['target_name']}** {t['target_price']} "
                           f"_(set {t['timestamp']})_\n")

    # Format key levels
    levels_text = "**📍 KEY LEVELS:**\n"
    level_map = {
        "PMH": weekly_state.get("pmh"),
        "PML": weekly_state.get("pml"),
        "PWH": weekly_state.get("pwh"),
        "PWL": weekly_state.get("pwl"),
        "PDH": weekly_state.get("pdh"),
        "PDL": weekly_state.get("pdl"),
        "ASH": weekly_state.get("asian_high"),
        "ASL": weekly_state.get("asian_low"),
        "NYPM-H": weekly_state.get("nypm_high"),
        "NYPM-L": weekly_state.get("nypm_low"),
    }
    for name, price in level_map.items():
        if price:
            levels_text += f"**{name}:** {price}\n"

    # Format profile status
    profile_text = f"**📊 WEEKLY PROFILE:** "
    if profile["profile"]:
        status_emoji = {"watching": "👀", "possible": "⚠️", 
                       "confirmed": "✅", "invalidated": "❌"}
        profile_text += (f"{profile['profile'].replace('_', ' ').upper()} "
                        f"{status_emoji.get(profile['status'], '❓')} {profile['status'].upper()}\n")
        for note in profile["notes"]:
            profile_text += f"→ {note}\n"
    else:
        profile_text += "Developing — waiting for Monday close\n"

    # Format weekly candle tracker
    days = ["monday", "tuesday", "wednesday", "thursday", "friday"]
    tracker_text = "**📅 WEEKLY CANDLE TRACKER:**\n"
    for d in days:
        data = weekly_state.get(d, {})
        if data.get("direction"):
            emoji = "🟢" if data["direction"] == "bullish" else "🔴"
            large = " ⚡LARGE" if data.get("large_expansion") else ""
            tracker_text += f"{emoji} {d.capitalize()}: {data['direction'].upper()}{large}\n"
        else:
            tracker_text += f"⬜ {d.capitalize()}: Not yet printed\n"

    # Format confluence checklist
    checklist_text = f"**✅ CONFLUENCE CHECKLIST — {score}/100:**\n"
    for item in checklist:
        checklist_text += f"{item}\n"

    # Format premium/discount
    pd_text = "**📈 MARKET CONTEXT:**\n"
    if isinstance(pd_status, dict):
        midpoint = pd_status["midpoint"]
        pd_text += (f"Range: {pd_status['range_label']}\n"
                   f"High: {pd_status['high']} | Low: {pd_status['low']}\n"
                   f"50% Midpoint: {midpoint}\n"
                   f"Bias: {weekly_state.get('bias', 'Not set').upper() if weekly_state.get('bias') else 'Not set'}\n")
    else:
        pd_text += f"{pd_status}\n"

    # Build full message
    message = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
☀️ **MORNING BRIEF — {day_name} {date_str}**
🇬🇧 London: {london_now.strftime('%H:%M')} | 🗽 NY: {ny_now.strftime('%H:%M')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📌 **TODAY:** {day_context.get(day_of_week, 'Weekend — prepare for next week')}

{pd_text}
{tracker_text}
{profile_text}
{levels_text}
{targets_text}
{checklist_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💬 **COMMANDS:**
!update [day] [direction] [large/small] — Update daily candle
!setlevel [name] [price] — Set a key level (e.g. !setlevel PDL 19888)
!setbias [bullish/bearish] — Set weekly bias
!c2confirmed — Mark C2 as confirmed
!alert — Check active alerts
!help — All commands
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    return message


def format_level_alert(level_name, price, direction, profile_context):
    """
    Formats the two-stage level alert:
    Stage 1: Sweep in progress — watching
    Stage 2: Candle closed back inside — confirmed
    """
    target_name, target_price = get_sequential_target_for_level(level_name)
    
    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔔 **KEY LEVEL CROSSED — {level_name}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**Level:** {level_name} @ {price}
**Direction crossed:** {direction}
**Profile context:** {profile_context}

⏳ **WATCHING FOR CANDLE CLOSE...**
→ Drop to H1 now
→ Check: FVG, OB, orderflow shift
→ Check SMT: Does ES/DOW hold higher low vs NQ?
→ Wait for H1 candle to CLOSE back inside {level_name}

**📍 SEQUENTIAL TARGET IF CONFIRMED:**
{level_name} {price} →→→ **{target_name}** {target_price if target_price else 'Set level first'}

_React ✅ when candle closes back inside to confirm C2_
_React ❌ if candle closes outside (sweep failed)_
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


def format_c2_confirmed_alert(level_name, price, entry_type=None):
    """
    Fires when the sweep candle closes back inside the level.
    This is the confirmed entry signal.
    """
    target_name, target_price = get_sequential_target_for_level(level_name)
    add_sequential_target(level_name, price, target_name, target_price)

    entry_guidance = ""
    if entry_type == 1:
        entry_guidance = ("**TYPE 1 ENTRY:** H1 swept and closed bullish back inside.\n"
                         "→ Enter on next H1 candle open\n"
                         "→ Stop: Below the sweep wick")
    elif entry_type == 2:
        entry_guidance = ("**TYPE 2 ENTRY:** Bearish H1 candle resting at level.\n"
                         "→ Enter on next H1 candle open\n"
                         "→ Stop: Below the range low")
    elif entry_type == 3:
        entry_guidance = ("⭐ **TYPE 3 ENTRY (BEST):** Closed below, next candle recovering.\n"
                         "→ Drop to 1 MINUTE chart NOW\n"
                         "→ Thin black line marked at sweep low\n"
                         "→ Enter when 1M candle closes ABOVE that line\n"
                         "→ Stop: Just below sweep low — TIGHT STOP = MAX SIZE")

    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ **C2 CONFIRMED — {level_name} SWEEP VALIDATED**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**Level swept:** {level_name} @ {price}
**Status:** Candle CLOSED BACK INSIDE ✅

{entry_guidance}

**🎯 SEQUENTIAL TARGET NOW ACTIVE:**
**{level_name}** {price} →→→ **{target_name}** {target_price if target_price else 'Not set'}
_(This target remains pinned until {target_name} is reached)_

**Confluence check before entry:**
☐ SMT confirmed vs ES/DOW?
☐ H1 orderflow shifted?
☐ FVG or OB in zone?
☐ News context clear?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


def format_thursday_counter_alert():
    """Special high priority alert for Thursday Counter setup."""
    wed = weekly_state["wednesday"]
    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚡ **THURSDAY COUNTER MODEL IN PLAY**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Mon ✅ Tue ✅ Wed ✅ — THREE consecutive expansion days confirmed.

**Thursday (C2) is YOUR day.**
Watch for Thursday to sweep Wednesday's 
{'LOW' if wed.get('direction') == 'bearish' else 'HIGH'}

**Alert level set:** Wednesday {'Low' if wed.get('direction') == 'bearish' else 'High'}: {wed.get('low') or wed.get('high')}

**When Thursday sweeps that level:**
→ Wait for H1 candle to CLOSE BACK INSIDE
→ Check SMT vs ES/DOW (this is critical)
→ Friday (C3) is your expansion target

**THIS IS THE TRADE YOU NEVER WANT TO MISS.** 🎯
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


def format_red_folder_warning(tomorrow_events):
    """Warning when today's setup precedes a red folder news event tomorrow."""
    event_names = ", ".join([e.get("name", "Unknown") for e in tomorrow_events])
    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ **RED FOLDER WARNING**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Setup forming today BUT tomorrow has:
🔴 **{event_names}**

**HIGH PROBABILITY:** Today's candle may NOT expand.
The real move is likely reserved for the news event tomorrow.

**Your options:**
1. **Wait** — trade the news day move instead (recommended)
2. **If you enter today** — take partial profits, expect chop
3. Treat today as **manipulation**, tomorrow as **distribution**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


# ─────────────────────────────────────────────
# DISCORD COMMANDS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Trading Coach Bot online — logged in as {bot.user}")
    morning_brief.start()
    sunday_check.start()
    print("Scheduled tasks started.")


@bot.command(name="brief")
async def send_brief(ctx):
    """Manually trigger the morning brief."""
    channel = discord.utils.get(ctx.guild.text_channels, name=BRIEF_CHANNEL)
    if channel:
        await channel.send(format_morning_brief())
    else:
        await ctx.send(format_morning_brief())


@bot.command(name="update")
async def update_candle(ctx, day: str, direction: str, size: str = "small"):
    """
    Update a daily candle's direction and size.
    Usage: !update monday bullish large
           !update tuesday bearish small
    """
    day = day.lower()
    direction = direction.lower()
    size = size.lower()

    valid_days = ["monday", "tuesday", "wednesday", "thursday", "friday"]
    valid_directions = ["bullish", "bearish"]

    if day not in valid_days:
        await ctx.send(f"❌ Invalid day. Use: {', '.join(valid_days)}")
        return

    if direction not in valid_directions:
        await ctx.send(f"❌ Invalid direction. Use: bullish or bearish")
        return

    weekly_state[day]["direction"] = direction
    weekly_state[day]["large_expansion"] = size == "large"

    profile = determine_weekly_profile()
    
    response = f"✅ **{day.capitalize()}** updated: {direction.upper()} ({'LARGE' if size == 'large' else 'small'} range)\n\n"
    
    if profile["notes"]:
        response += "**📊 Profile Update:**\n"
        for note in profile["notes"]:
            response += f"→ {note}\n"

    # Check for Thursday Counter
    if profile["profile"] == "thursday_counter" and day == "wednesday":
        alerts_channel = discord.utils.get(ctx.guild.text_channels, name=ALERTS_CHANNEL)
        if alerts_channel:
            await alerts_channel.send(format_thursday_counter_alert())

    await ctx.send(response)


@bot.command(name="setlevel")
async def set_level(ctx, level_name: str, price: float):
    """
    Set a key level price.
    Usage: !setlevel PDL 19888
           !setlevel PWH 20104
           !setlevel PDH 20015
    """
    level_map = {
        "PMH": "pmh", "PML": "pml",
        "PWH": "pwh", "PWL": "pwl",
        "PDH": "pdh", "PDL": "pdl",
        "ASH": "asian_high", "ASL": "asian_low",
        "NYPM-H": "nypm_high", "NYPM-L": "nypm_low",
        "WEEKLYOPEN": "weekly_open"
    }

    level_key = level_map.get(level_name.upper())
    if not level_key:
        await ctx.send(f"❌ Unknown level. Valid levels: {', '.join(level_map.keys())}")
        return

    weekly_state[level_key] = price
    target_name, target_price = get_sequential_target_for_level(level_name.upper())

    response = f"✅ **{level_name.upper()}** set at **{price}**\n"
    if target_price:
        response += f"📍 Sequential target: {level_name.upper()} → **{target_name}** @ {target_price}"
    else:
        response += f"📍 Sequential target: {level_name.upper()} → **{target_name}** _(set {target_name} to activate)_"

    await ctx.send(response)


@bot.command(name="setbias")
async def set_bias(ctx, bias: str):
    """
    Set the weekly directional bias.
    Usage: !setbias bullish
           !setbias bearish
    """
    bias = bias.lower()
    if bias not in ["bullish", "bearish"]:
        await ctx.send("❌ Use: !setbias bullish OR !setbias bearish")
        return

    weekly_state["bias"] = bias
    emoji = "🟢" if bias == "bullish" else "🔴"
    await ctx.send(f"{emoji} Weekly bias set to **{bias.upper()}**")


@bot.command(name="c2confirmed")
async def confirm_c2(ctx, level_name: str = "KEY LEVEL", 
                     price: float = 0, entry_type: int = 1):
    """
    Mark C2 as confirmed — sweep candle closed back inside.
    Usage: !c2confirmed PDL 19888 3
    """
    weekly_state["c2_confirmed"] = True
    alerts_channel = discord.utils.get(ctx.guild.text_channels, name=ALERTS_CHANNEL)
    
    msg = format_c2_confirmed_alert(level_name.upper(), price, entry_type)
    
    if alerts_channel:
        await alerts_channel.send(msg)
    await ctx.send(msg)


@bot.command(name="levelhit")
async def level_hit(ctx, level_name: str, price: float, direction: str = "below"):
    """
    Alert that a key level has been crossed — starts the watching phase.
    Usage: !levelhit PDL 19888 below
    """
    profile = determine_weekly_profile()
    profile_context = (f"{profile['profile'].replace('_', ' ').title()} — "
                      f"{profile['status'].upper()}" if profile["profile"] 
                      else "Profile developing")

    msg = format_level_alert(level_name.upper(), price, direction, profile_context)
    
    alerts_channel = discord.utils.get(ctx.guild.text_channels, name=ALERTS_CHANNEL)
    if alerts_channel:
        await alerts_channel.send(msg)
    await ctx.send(f"✅ Level alert posted to #{ALERTS_CHANNEL}")


@bot.command(name="alert")
async def check_alerts(ctx):
    """Show all active alerts and sequential targets."""
    active_targets = [t for t in weekly_state["sequential_targets"] if t["active"]]
    
    if not active_targets:
        await ctx.send("📭 No active sequential targets at the moment.")
        return

    msg = "**🎯 ACTIVE SEQUENTIAL TARGETS:**\n━━━━━━━━━━━━━━━━━━━━━\n"
    for t in active_targets:
        msg += (f"**{t['level_name']}** {t['level_price']} "
               f"→→→ **{t['target_name']}** {t['target_price']}\n"
               f"_Set: {t['timestamp']}_\n\n")
    
    await ctx.send(msg)


@bot.command(name="targethit")
async def target_hit(ctx, target_name: str):
    """
    Mark a sequential target as reached — removes it from active targets.
    Usage: !targethit PWH
    """
    for t in weekly_state["sequential_targets"]:
        if t["target_name"].upper() == target_name.upper() and t["active"]:
            t["active"] = False
            await ctx.send(
                f"🏆 **TARGET REACHED — {target_name.upper()}**\n"
                f"Sequential target from {t['level_name']} @ {t['level_price']} "
                f"→ {t['target_name']} @ {t['target_price']} **COMPLETE** ✅"
            )
            return
    
    await ctx.send(f"❌ No active target found for {target_name.upper()}")


@bot.command(name="profile")
async def show_profile(ctx):
    """Show current weekly profile status."""
    profile = determine_weekly_profile()
    
    msg = f"**📊 WEEKLY PROFILE STATUS:**\n━━━━━━━━━━━━━━━━━━━━━\n"
    if profile["profile"]:
        msg += f"**Profile:** {profile['profile'].replace('_', ' ').upper()}\n"
        msg += f"**Status:** {profile['status'].upper()}\n"
        msg += f"**C2 Day:** {profile.get('c2_day', 'TBD').capitalize()}\n"
        msg += f"**Expansion Days:** {', '.join(profile.get('expansion_days', []))}\n\n"
        for note in profile["notes"]:
            msg += f"→ {note}\n"
    else:
        msg += "Profile not yet determined — update daily candles first.\n"
        msg += "Use: !update [day] [bullish/bearish] [large/small]"
    
    await ctx.send(msg)


@bot.command(name="score")
async def show_score(ctx):
    """Show current confluence score and checklist."""
    score, checklist = get_confluence_score()
    
    msg = f"**✅ CONFLUENCE SCORE: {score}/100**\n━━━━━━━━━━━━━━━━━━━━━\n"
    for item in checklist:
        msg += f"{item}\n"
    
    color_guide = ""
    if score >= 80:
        color_guide = "🟢 HIGH CONFLUENCE — Setup looks strong"
    elif score >= 60:
        color_guide = "🟡 MODERATE — Waiting on more confirmation"
    else:
        color_guide = "🔴 LOW — Not enough confluence yet, wait"
    
    msg += f"\n{color_guide}"
    await ctx.send(msg)


@bot.command(name="redfolder")
async def red_folder_warning(ctx, *, event_name: str):
    """
    Post a red folder warning for tomorrow.
    Usage: !redfolder FOMC Rate Decision
    """
    events = [{"name": event_name}]
    msg = format_red_folder_warning(events)
    
    alerts_channel = discord.utils.get(ctx.guild.text_channels, name=ALERTS_CHANNEL)
    if alerts_channel:
        await alerts_channel.send(msg)
    await ctx.send(f"⚠️ Red folder warning posted to #{ALERTS_CHANNEL}")


@bot.command(name="resetweek")
async def reset_week(ctx):
    """Reset all weekly data for a new week — run every Sunday."""
    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        weekly_state[day] = {
            "range": None, "direction": None, 
            "high": None, "low": None, "large_expansion": False
        }
    weekly_state["profile"] = None
    weekly_state["c2_confirmed"] = False
    weekly_state["active_alerts"] = []
    weekly_state["sequential_targets"] = []
    
    await ctx.send(
        "🔄 **Weekly data reset.** New week ready.\n"
        "Don't forget to:\n"
        "→ !setbias [bullish/bearish] — Set your weekly bias\n"
        "→ !setlevel PWH [price] — Set previous week high\n"
        "→ !setlevel PWL [price] — Set previous week low\n"
        "→ !setlevel PMH [price] — Set previous month high\n"
        "→ !setlevel PML [price] — Set previous month low"
    )


@bot.command(name="help")
async def help_command(ctx):
    """Show all available commands."""
    msg = """
**📚 TRADING COACH — ALL COMMANDS**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**DAILY UPDATES:**
`!brief` — Trigger morning brief manually
`!update [day] [direction] [size]` — Update daily candle
  _e.g. !update monday bearish large_
`!setbias [bullish/bearish]` — Set weekly bias

**KEY LEVELS:**
`!setlevel [name] [price]` — Set a key level
  _Valid: PMH, PML, PWH, PWL, PDH, PDL, ASH, ASL, NYPM-H, NYPM-L_
  _e.g. !setlevel PDL 19888_

**ALERTS & ENTRIES:**
`!levelhit [level] [price] [direction]` — Key level crossed
  _e.g. !levelhit PDL 19888 below_
`!c2confirmed [level] [price] [type]` — C2 sweep confirmed
  _e.g. !c2confirmed PDL 19888 3_
`!targethit [target]` — Mark sequential target as reached
  _e.g. !targethit PWH_

**ANALYSIS:**
`!profile` — Show weekly profile status
`!score` — Show confluence score
`!alert` — Show active sequential targets
`!redfolder [event name]` — Post red folder warning

**WEEKLY RESET:**
`!resetweek` — Reset for new week (run Sunday)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    await ctx.send(msg)


# ─────────────────────────────────────────────
# SCHEDULED TASKS
# ─────────────────────────────────────────────
@tasks.loop(time=time(6, 30, tzinfo=LONDON_TZ))
async def morning_brief():
    """Send morning brief at 6:30am London time every weekday."""
    london_now = datetime.now(LONDON_TZ)
    if london_now.weekday() >= 5:  # Skip weekends
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = discord.utils.get(guild.text_channels, name=BRIEF_CHANNEL)
    if channel:
        await channel.send(format_morning_brief())


@tasks.loop(time=time(20, 0, tzinfo=LONDON_TZ))
async def sunday_check():
    """Sunday evening reminder to complete weekly analysis."""
    london_now = datetime.now(LONDON_TZ)
    if london_now.weekday() != 6:  # Only Sunday
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = discord.utils.get(guild.text_channels, name=BRIEF_CHANNEL)
    if channel:
        await channel.send("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 **SUNDAY PREP — WEEKLY ANALYSIS REMINDER**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Time to build your narrative for the week ahead.

**Complete your top-down analysis:**
☐ Monthly chart — where is price drawing to?
☐ Weekly chart — PMH/PML/PWH/PWL identified?
☐ Daily chart — bias confirmed?
☐ Are we in Accumulation, Manipulation or Distribution?
☐ Active FVGs near current price noted?
☐ Economic calendar checked for red folder events?

**Then update the bot:**
→ `!setbias [bullish/bearish]`
→ `!setlevel PWH [price]`
→ `!setlevel PWL [price]`
→ `!setlevel PMH [price]`
→ `!setlevel PML [price]`
→ `!resetweek` if starting fresh

**Reply here with your bias and I'll confirm your levels for Monday.**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


# ─────────────────────────────────────────────
# RUN BOT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
