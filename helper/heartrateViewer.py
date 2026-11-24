import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import pandas as pd
import json
import os
import re
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

# Dark Theme Palette
BG_MAIN = "#121212"
BG_PANEL = "#1e1e1e"
BG_ACCENT = "#252526"
FG_TEXT = "#e0e0e0"
FG_MUTED = "#888888"
GRID_COLOR = "#333333"
HR_COLOR = "#ff5252"
RR_COLOR = "#40c4ff"

class DarkNavigationToolbar(NavigationToolbar2Tk):
    def __init__(self, canvas, window):
        super().__init__(canvas, window)
        self.config(background=BG_PANEL)
        self._message_label.config(background=BG_PANEL, foreground=FG_TEXT)
        for widget in self.winfo_children():
            if isinstance(widget, (tk.Button, tk.Checkbutton)):
                widget.config(background=BG_ACCENT, foreground=FG_TEXT, activebackground="#3e3e42")
class HeartRateViewer:
    def __init__(self, root):
        self.root = root
        self.root.title("HeartRate & RR Session Viewer")
        self.root.geometry("1050x720")
        self.root.configure(bg=BG_MAIN)

        self.x_labels = []
        self.hr_values = []
        self.rr_values = []
        self.window_size = 50

        self.apply_dark_ttk_theme()

        # Top control bar
        top_frame = tk.Frame(root, bg=BG_MAIN, pady=10, padx=12)
        top_frame.pack(fill=tk.X)

        btn_load = tk.Button(
            top_frame, text="Open Report (TXT, CSV, JSON)", command=self.load_file,
            bg="#007acc", fg="white", activebackground="#005f9e", activeforeground="white",
            relief=tk.FLAT, padx=14, pady=5, font=("Segoe UI", 9, "bold"), cursor="hand2"
        )
        btn_load.pack(side=tk.LEFT)

        self.lbl_file = tk.Label(top_frame, text="No file loaded", bg=BG_MAIN, fg=FG_MUTED, padx=15, font=("Segoe UI", 9))
        self.lbl_file.pack(side=tk.LEFT)

        lbl_hint = tk.Label(top_frame, text="Tip: Scroll wheel = Pan  |  Ctrl + Scroll = Zoom", bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 8))
        lbl_hint.pack(side=tk.RIGHT)

        # Metadata banner
        self.lbl_meta = tk.Label(
            root, text="Session Metadata will appear here",
            justify=tk.LEFT, anchor="w", padx=12, pady=8,
            bg=BG_PANEL, fg="#a6d8ff", font=("Consolas", 9)
        )
        self.lbl_meta.pack(fill=tk.X, padx=12)

        # Summary stats bar
        self.lbl_stats = tk.Label(
            root, text="Avg HR: -- bpm  |  Mean RR: -- ms  |  Total Beats (RR): 0",
            font=("Segoe UI", 10, "bold"), pady=8, bg=BG_ACCENT, fg=FG_TEXT
        )
        self.lbl_stats.pack(fill=tk.X, padx=12, pady=(6, 8))

        # Tabbed Notebook
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        self.tab_graph = tk.Frame(self.notebook, bg=BG_PANEL)
        self.tab_table = tk.Frame(self.notebook, bg=BG_PANEL)

        self.notebook.add(self.tab_graph, text="  Interactive Graph  ")
        self.notebook.add(self.tab_table, text="  Data Table  ")

        self.setup_graph_tab()
        self.setup_table_tab()

    def apply_dark_ttk_theme(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background=BG_MAIN, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_PANEL, foreground=FG_MUTED, padding=[12, 6], font=("Segoe UI", 9, "bold"))
        style.map("TNotebook.Tab", background=[("selected", BG_ACCENT)], foreground=[("selected", "white")])

        style.configure("Treeview", background=BG_PANEL, foreground=FG_TEXT, fieldbackground=BG_PANEL, rowheight=26, borderwidth=0)
        style.configure("Treeview.Heading", background=BG_ACCENT, foreground="white", font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#007acc")])

        style.configure("Horizontal.TScrollbar", background=BG_ACCENT, troughcolor=BG_MAIN, bordercolor=BG_MAIN, arrowcolor=FG_TEXT)
        style.configure("Vertical.TScrollbar", background=BG_ACCENT, troughcolor=BG_MAIN, bordercolor=BG_MAIN, arrowcolor=FG_TEXT)

    def setup_graph_tab(self):
        self.fig = Figure(figsize=(10, 5), dpi=100, facecolor=BG_PANEL)
        self.ax_hr = self.fig.add_subplot(211)
        self.ax_rr = self.fig.add_subplot(212)
        self.style_axes()

        self.mpl_canvas = FigureCanvasTkAgg(self.fig, master=self.tab_graph)
        self.mpl_widget = self.mpl_canvas.get_tk_widget()
        self.mpl_widget.configure(bg=BG_PANEL, highlightthickness=0)
        self.mpl_widget.pack(fill=tk.BOTH, expand=True)

        self.graph_scroll = ttk.Scrollbar(self.tab_graph, orient=tk.HORIZONTAL, command=self.on_scrollbar)
        self.graph_scroll.pack(fill=tk.X, padx=10, pady=(0, 4))

        self.mpl_canvas.mpl_connect("scroll_event", self.on_mouse_wheel)

        toolbar = DarkNavigationToolbar(self.mpl_canvas, self.tab_graph)
        toolbar.update()

    def style_axes(self):
        for ax in (self.ax_hr, self.ax_rr):
            ax.set_facecolor(BG_MAIN)
            ax.tick_params(colors=FG_TEXT, labelsize=8)
            ax.xaxis.label.set_color(FG_TEXT)
            ax.yaxis.label.set_color(FG_TEXT)
            ax.title.set_color("white")
            for spine in ax.spines.values():
                spine.set_color(GRID_COLOR)
            ax.grid(True, linestyle="--", color=GRID_COLOR, alpha=0.7)

    def setup_table_tab(self):
        scroll_y = ttk.Scrollbar(self.tab_table, orient=tk.VERTICAL)
        scroll_x = ttk.Scrollbar(self.tab_table, orient=tk.HORIZONTAL)

        self.tree = ttk.Treeview(self.tab_table, yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        scroll_y.config(command=self.tree.yview)
        scroll_x.config(command=self.tree.xview)

        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        scroll_x.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(fill=tk.BOTH, expand=True)

    def parse_heartrate_txt(self, filepath):
        meta_lines = []
        rows = []
        row_pattern = re.compile(
            r"UTC:\s*(?P<utc>[^|]+)\|\s*Local:\s*(?P<local>[^|]+)\|\s*(?P<bpm>\d+)\s*BPM\s*\|\s*RR:\s*\[(?P<rr>[^\]]*)\]"
        )

        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("---"):
                    continue
                match = row_pattern.match(line)
                if match:
                    rr_raw = match.group("rr").strip()
                    rr_list = [float(x.strip()) for x in rr_raw.split(",") if x.strip()] if rr_raw else []
                    rows.append({
                        "Local Time": match.group("local").strip(),
                        "UTC Time": match.group("utc").strip(),
                        "HR (BPM)": int(match.group("bpm")),
                        "RR Intervals (ms)": ", ".join(map(str, rr_list)) if rr_list else "None",
                        "_rr_values": rr_list
                    })
                else:
                    meta_lines.append(line)

        meta_text = "\n".join(meta_lines) if meta_lines else "Standard text file loaded."
        return pd.DataFrame(rows), meta_text
    def load_file(self):
        filepath = filedialog.askopenfilename(
            title="Select HeartRate Data File",
            filetypes=[("Supported Files", "*.txt *.csv *.json"), ("All Files", "*.*")]
        )
        if not filepath:
            return

        try:
            _, ext = os.path.splitext(filepath)
            ext = ext.lower()
            meta_text = f"Loaded {ext.upper()} file"

            if ext == '.txt':
                df, meta_text = self.parse_heartrate_txt(filepath)
            elif ext == '.csv':
                df = pd.read_csv(filepath)
            elif ext == '.json':
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                df = pd.json_normalize(data) if isinstance(data, list) else pd.DataFrame(data)
            else:
                raise ValueError("Unsupported file format.")

            self.display_data(df, os.path.basename(filepath), meta_text)

        except Exception as e:
            messagebox.showerror("Error Loading File", f"Could not parse file:\n{e}")

    def display_data(self, df, filename, meta_text):
        self.lbl_file.config(text=filename, fg=FG_TEXT)
        self.lbl_meta.config(text=meta_text)

        visible_cols = [c for c in df.columns if not c.startswith("_")]
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = visible_cols
        self.tree["show"] = "headings"

        for col in visible_cols:
            self.tree.heading(col, text=col)
            width = 230 if "Time" in col else 140
            self.tree.column(col, width=width, anchor=tk.CENTER)

        for _, row in df.iterrows():
            self.tree.insert("", tk.END, values=[row[c] for c in visible_cols])

        self.update_stats_and_plots(df)

    def update_stats_and_plots(self, df):
        hr_col = next((c for c in df.columns if 'hr' in c.lower() or 'bpm' in c.lower()), None)
        hr_series = pd.to_numeric(df[hr_col], errors='coerce').dropna() if hr_col else pd.Series(dtype=float)
        self.hr_values = hr_series.tolist()
        avg_hr = f"{hr_series.mean():.1f}" if not hr_series.empty else "--"

        time_col = next((c for c in df.columns if 'local' in c.lower() or 'time' in c.lower() or 'utc' in c.lower()), None)
        if time_col:
            self.x_labels = [str(t).split("T")[-1][:8] if "T" in str(t) else str(t) for t in df[time_col]]
        else:
            self.x_labels = [str(i) for i in range(len(df))]

        self.rr_values = []
        if "_rr_values" in df.columns:
            for sublist in df["_rr_values"]:
                self.rr_values.extend(sublist)
        else:
            rr_col = next((c for c in df.columns if 'rr' in c.lower()), None)
            if rr_col:
                self.rr_values = pd.to_numeric(df[rr_col], errors='coerce').dropna().tolist()

        mean_rr = f"{sum(self.rr_values)/len(self.rr_values):.1f}" if self.rr_values else "--"
        self.lbl_stats.config(text=f"Avg HR: {avg_hr} bpm  |  Mean RR: {mean_rr} ms  |  Total Beats (RR): {len(self.rr_values)}")

        self.ax_hr.clear()
        if self.hr_values:
            self.ax_hr.plot(range(len(self.hr_values)), self.hr_values, color=HR_COLOR, marker="o", markersize=4, linewidth=1.6)
        self.ax_hr.set_title("Heart Rate (BPM)", fontsize=10, fontweight="bold")
        self.ax_hr.set_ylabel("BPM")

        self.ax_rr.clear()
        if self.rr_values:
            self.ax_rr.plot(range(len(self.rr_values)), self.rr_values, color=RR_COLOR, marker="o", markersize=3.5, linewidth=1.4)
        self.ax_rr.set_title("RR Intervals (ms)", fontsize=10, fontweight="bold")
        self.ax_rr.set_ylabel("ms")
        self.ax_rr.set_xlabel("Beat / Sample Sequence")

        self.style_axes()
        self.fig.tight_layout(pad=2.5)

        self.window_size = min(50, max(len(self.hr_values), len(self.rr_values), 5))
        self.set_viewport(0, self.window_size)

    def set_viewport(self, start, end):
        max_hr = max(len(self.hr_values) - 1, 1)
        max_rr = max(len(self.rr_values) - 1, 1)
        total_max = max(max_hr, max_rr)

        width = end - start
        start = max(0, min(start, total_max - width))
        end = min(total_max, start + width)

        self.ax_hr.set_xlim(start - 0.2, min(end, max_hr) + 0.2)
        self.ax_rr.set_xlim(start - 0.2, min(end, max_rr) + 0.2)

        if self.x_labels:
            vis_start = max(0, int(start))
            vis_end = min(len(self.x_labels) - 1, int(end))
            step = max(1, (vis_end - vis_start) // 10)
            ticks = list(range(vis_start, vis_end + 1, step))
            self.ax_hr.set_xticks(ticks)
            self.ax_hr.set_xticklabels([self.x_labels[i] for i in ticks], rotation=15, ha="right", fontsize=8)

        if total_max > 0:
            self.graph_scroll.set(start / total_max, end / total_max)
        else:
            self.graph_scroll.set(0, 1)

        self.mpl_canvas.draw_idle()

    def on_scrollbar(self, action, *args):
        start, end = self.ax_hr.get_xlim()
        start, end = start + 0.2, end - 0.2
        width = end - start
        total_max = max(len(self.hr_values) - 1, len(self.rr_values) - 1, 1)

        if action == "moveto":
            new_start = float(args[0]) * total_max
            self.set_viewport(new_start, new_start + width)
        elif action == "scroll":
            step = int(args[0]) * max(1, width * 0.15)
            self.set_viewport(start + step, end + step)

    def on_mouse_wheel(self, event):
        if not self.hr_values and not self.rr_values:
            return

        start, end = self.ax_hr.get_xlim()
        start, end = start + 0.2, end - 0.2
        width = end - start
        direction = -1 if event.button == "up" else 1

        if event.key == "control":
            zoom_factor = 0.8 if event.button == "up" else 1.25
            new_width = max(3, width * zoom_factor)
            center = event.xdata if event.xdata is not None else (start + end) / 2
            self.set_viewport(center - new_width / 2, center + new_width / 2)
        else:
            shift = direction * max(0.5, width * 0.15)
            self.set_viewport(start + shift, end + shift)

if __name__ == "__main__":
    root = tk.Tk()
    app = HeartRateViewer(root)
    root.mainloop()
