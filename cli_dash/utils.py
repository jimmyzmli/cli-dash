from datetime import datetime, timedelta
from typing import Optional

def get_next_cron_run(cron_expr: str, base_time: datetime) -> datetime:
    """
    Calculate the next run time for a cron expression, supporting L (last) extensions.
    - L in DOM field: Last day of the month (native croniter)
    - LW in DOM field: Last weekday of the month
    - dL in DOW field: Last day-of-week d of the month (e.g. 5L for last Friday)
    """
    from croniter import croniter
    
    # Normalize
    cron_expr = cron_expr.strip()
    fields = cron_expr.split()
    if len(fields) != 5:
        return croniter(cron_expr, base_time).get_next(datetime)
    
    dom = fields[2].upper()
    dow = fields[4].upper()
    
    # 1. Handle LW (Last Weekday of month) in DOM field
    if dom == "LW":
        # Strategy: Use croniter with 'L' in DOM and the original minutes/hours.
        # If the result is a weekend, adjust it back.
        # If the adjusted result is <= base_time, get the NEXT 'L' and adjust again.
        new_expr = " ".join(fields[:2] + ["L"] + fields[3:])
        it = croniter(new_expr, base_time)
        while True:
            candidate = it.get_next(datetime)
            lw = candidate
            if lw.weekday() == 5: # Saturday
                lw -= timedelta(days=1)
            elif lw.weekday() == 6: # Sunday
                lw -= timedelta(days=2)
            
            if lw > base_time:
                return lw

    # 2. Handle dL (Last day-of-week d) in DOW field
    if dow.endswith('L') and len(dow) > 1 and dow[:-1].isdigit():
        target_dow = dow[:-1]
        new_expr = " ".join(fields[:4] + [target_dow])
        it = croniter(new_expr, base_time)
        while True:
            candidate = it.get_next(datetime)
            # Check if adding 7 days moves it to a different month
            if (candidate + timedelta(days=7)).month != candidate.month:
                return candidate

    # 3. Native croniter (handles L in DOM automatically)
    return croniter(cron_expr, base_time).get_next(datetime)
