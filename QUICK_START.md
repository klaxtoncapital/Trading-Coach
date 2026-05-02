# TRADING COACH BOT — QUICK START
# ════════════════════════════════

## BEFORE YOU START
Make sure you have Python installed.
Check by opening Terminal (Mac) or Command Prompt (Windows) and typing:
    python --version

If you see Python 3.x.x you're good.
If not, download Python from: python.org/downloads

## CREATE YOUR DISCORD CHANNELS
In your Discord server "klaxtoncapital's server" create these 3 channels:
- #morning-brief
- #alerts  
- #trade-journal

## RUNNING THE BOT

### On Windows:
1. Unzip the trading_coach_bot folder
2. Double click "start.bat"
3. A black window opens — you should see "Trading Coach Bot online"
4. Leave that window open — the bot runs as long as it's open

### On Mac:
1. Unzip the trading_coach_bot folder
2. Open Terminal
3. Type: cd [drag the folder into terminal]
4. Type: sh start.sh
5. You should see "Trading Coach Bot online"
6. Leave Terminal open

## TEST IT'S WORKING
In any channel in your Discord server type:
    !brief

You should get your first morning brief back immediately!

## FIRST THINGS TO DO
Once the bot is running, go to your #morning-brief channel and type:

Set your weekly bias:
    !setbias bullish

Set your key levels (use current NQ prices):
    !setlevel PWH 20104
    !setlevel PWL 19650
    !setlevel PDH 19980
    !setlevel PDL 19820

Test the profile tracker:
    !update monday bearish small
    !update tuesday bearish small
    !profile

Test an alert:
    !levelhit PDL 19820 below

## KEEPING THE BOT RUNNING 24/7
The bot only runs while your computer is on and the window is open.
For 24/7 running we'll set up a cloud server in Phase 2.
For now, just turn it on each morning before the market opens.

## NEED HELP?
All commands: !help
