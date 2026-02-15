"""Weather display rendering module for ePaper."""
import framebuf

# Layout constants - all hardcoded positions in one place
LAYOUT = {
    "today_header_y": 8,
    "today_header_x": 14,
    "main_icon_x": 20,
    "main_icon_y": 33,
    "divider_x": 115,
    "divider_y_start": 33,
    "divider_height": 60,
    "rain_icon_x": 130,
    "rain_text_x": 200,
    "rain_text_y": 53,
    "temp_start_y": 110,
    "temp_start_x": 14,
    "temp_spacing": 30,
    "temp_label_y_offset": 3,
    "temp_scale": 3,
    "forecast_divider_x": 300,
    "forecast_panel_x": 306,
    "forecast_card_h": 31,
    "forecast_card_gap": 4,
    "forecast_top_pad": 2,
    "forecast_divider_vpad": 10,
}


class WeatherDisplay:
    """Renders weather forecast data to framebuffer."""
    
    def __init__(self, fb_black, fb_yellow, draw_text_func, draw_icon_func, 
                 draw_bmp_icon_func, _month_name_func, bmp_width, bmp_height):
        """Initialize display renderer.
        
        Args:
            fb_black: Black plane framebuffer
            fb_yellow: Yellow plane framebuffer
            draw_text_func: _draw_text_compat function
            draw_icon_func: _draw_icon function
            draw_bmp_icon_func: draw_bmp_icon function
            _month_name_func: _month_name function
            bmp_width: BMP canvas width
            bmp_height: BMP canvas height
        """
        self.fb_black = fb_black
        self.fb_yellow = fb_yellow
        self.draw_text = draw_text_func
        self.draw_icon = draw_icon_func
        self.draw_bmp_icon = draw_bmp_icon_func
        self.month_name = _month_name_func
        self.bmp_width = bmp_width
        self.bmp_height = bmp_height
    
    def render(self, forecast_data):
        """Render complete weather display.
        
        Args:
            forecast_data: Dict with 'location', 'days' keys
        """
        days = forecast_data.get("days") or []
        
        if not days:
            self.draw_text(self.fb_black, 12, 12, "No forecast", 0)
            return
        
        today = days[0] if len(days) >= 1 else {}
        forecast_list = days[1:6] if len(days) >= 2 else []
        
        self._draw_forecast_panel(forecast_list)
        self._draw_today_panel(today)
    
    def _draw_forecast_panel(self, forecast_list):
        """Draw 5-day forecast on right side."""
        cfg = LAYOUT
        
        # Vertical divider
        self.fb_black.vline(cfg["forecast_divider_x"], cfg["forecast_divider_vpad"], 
                           self.bmp_height - (cfg["forecast_divider_vpad"] * 2), 0)
        
        for idx, day in enumerate(forecast_list):
            y0 = cfg["forecast_top_pad"] + idx * (cfg["forecast_card_h"] + cfg["forecast_card_gap"])
            
            # Horizontal divider between days
            if idx < len(forecast_list) - 1:
                line_y = y0 + cfg["forecast_card_h"] + (cfg["forecast_card_gap"] // 2)
                self.fb_black.hline(cfg["forecast_panel_x"], line_y, 
                                   self.bmp_width - cfg["forecast_panel_x"] - 8, 0)
            
            weekday = day.get("weekday", "---")
            high_txt = "%s°" % ("--" if day.get("temp_max") is None else str(day.get("temp_max")))
            low_txt = "%s°" % ("--" if day.get("temp_min") is None else str(day.get("temp_min")))
            
            self.draw_text(self.fb_black, cfg["forecast_panel_x"] + 6, y0 + 12, weekday, 0)
            self.draw_icon(self.fb_black, self.fb_yellow, cfg["forecast_panel_x"] + 50, y0, 
                          day.get("icon"), compact=True)
            self.draw_text(self.fb_black, cfg["forecast_panel_x"] + 90, y0 + 12, low_txt, 0)
            self.draw_text(self.fb_yellow, cfg["forecast_panel_x"] + 134, y0 + 12, high_txt, 1)
    
    def _draw_today_panel(self, today):
        """Draw today's detailed panel on left side."""
        cfg = LAYOUT
        
        # Header with date
        today_date_str = self._format_today_date(today)
        self.draw_text(self.fb_yellow, cfg["today_header_x"], cfg["today_header_y"], 
                      today_date_str, 1)
        
        # Main weather icon
        self.draw_icon(self.fb_black, self.fb_yellow, cfg["main_icon_x"], cfg["main_icon_y"], 
                      today.get("icon"))
        
        # Vertical divider
        self.fb_black.vline(cfg["divider_x"], cfg["divider_y_start"], cfg["divider_height"], 0)
        
        # Rain icon and value
        self._draw_rain_section(today)
        
        # Temperature readings
        self._draw_temp_section(today)
    
    def _format_today_date(self, today):
        """Format today's date string."""
        today_date_str = "TODAY"
        
        t_weekday = today.get("weekday", "")
        t_day_num = today.get("day_num", 0)
        t_month_num = today.get("month_num", 0)
        
        full_days = {
            "Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday",
            "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"
        }
        
        if t_weekday and t_day_num and t_month_num:
            full_day = full_days.get(t_weekday, t_weekday)
            abbr_month = self.month_name(t_month_num)[:3]
            today_date_str += " - %s %d %s" % (full_day, t_day_num, abbr_month)
        
        return today_date_str
    
    def _draw_rain_section(self, today):
        """Draw rain icon and value."""
        cfg = LAYOUT
        
        r_low = today.get("rain_lower", 0) or 0
        r_high = today.get("rain_upper", 0) or 0
        
        rain_txt = "%s mm" % (r_high if r_high else r_low)
        
        # Rain icon
        try:
            self.draw_bmp_icon(self.fb_black, self.fb_yellow, cfg["rain_icon_x"], 
                              cfg["main_icon_y"], "raindrops", "_b")
        except:
            pass  # Icon missing, skip
        
        # Rain value
        self.draw_text(self.fb_yellow, cfg["rain_text_x"], cfg["rain_text_y"], 
                      rain_txt, 1, scale=cfg["temp_scale"])
    
    def _draw_temp_section(self, today):
        """Draw temperature readings (High/Low or Now/Later)."""
        cfg = LAYOUT
        
        # Get labels and values
        now_data = today.get("now")
        
        if now_data:
            label_1 = str(now_data.get("now_label", "Now"))
            val_1 = "%s°" % now_data.get("temp_now", "--")
            label_2 = str(now_data.get("later_label", "Later"))
            val_2 = "%s°" % now_data.get("temp_later", "--")
        else:
            label_1 = "High"
            val_1 = "%s°" % ("--" if today.get("temp_max") is None else str(today.get("temp_max")))
            label_2 = "Low"
            val_2 = "%s°" % ("--" if today.get("temp_min") is None else str(today.get("temp_min")))
        
        # Calculate alignment
        lbl1_w = len(label_1) * 12
        lbl2_w = len(label_2) * 12
        max_lbl_w = max(lbl1_w, lbl2_w)
        val_x = cfg["temp_start_x"] + max_lbl_w + 12
        
        # Line 1
        y_stats = cfg["temp_start_y"]
        self.draw_text(self.fb_black, cfg["temp_start_x"], y_stats + cfg["temp_label_y_offset"], 
                      label_1, 0)
        self.draw_text(self.fb_yellow, val_x, y_stats, val_1, 1, scale=cfg["temp_scale"])
        
        # Line 2
        y_line2 = y_stats + cfg["temp_spacing"]
        self.draw_text(self.fb_black, cfg["temp_start_x"], y_line2 + cfg["temp_label_y_offset"], 
                      label_2, 0)
        self.draw_text(self.fb_yellow, val_x, y_line2, val_2, 1, scale=cfg["temp_scale"])
