import os
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

from .pipeline import ARTIFACTS_DIR, _slugify, run_full_pipeline


def _open_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except Exception as e:
        messagebox.showerror("无法打开目录", str(e))


class App(tk.Tk):
    """
    说明（给非技术同事）：
    - 启动页只收集 3 个输入：Key、txt、人物名（别名可选）
    - 点击一次，后台自动跑完整流程并输出三份文件
    """

    def __init__(self) -> None:
        super().__init__()
        self.title("DeepSeek 一键生成扮演指令（小说人物）")
        self.geometry("1020x720")

        self.api_key = tk.StringVar(value="")
        self.novel_path = tk.StringVar(value="")
        self.character_name = tk.StringVar(value="")
        self.aliases = tk.StringVar(value="")

        self.status = tk.StringVar(value="等待开始")
        self._worker: threading.Thread | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)

        self.setup_card = ttk.Frame(outer, padding=16, relief="solid")
        self.setup_card.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(self.setup_card, text="DeepSeek API Key").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.setup_card, textvariable=self.api_key, show="*", width=60).grid(
            row=0, column=1, sticky="we", padx=(10, 0)
        )
        ttk.Label(
            self.setup_card,
            text="（每次手动输入，不保存）",
            foreground="#888",
        ).grid(row=0, column=2, sticky="w", padx=(10, 0))

        ttk.Label(self.setup_card, text="小说 txt").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(self.setup_card, textvariable=self.novel_path, width=60).grid(
            row=1, column=1, sticky="we", padx=(10, 0), pady=(10, 0)
        )
        ttk.Button(self.setup_card, text="选择文件…", command=self._pick_novel).grid(
            row=1, column=2, sticky="e", padx=(10, 0), pady=(10, 0)
        )

        ttk.Label(self.setup_card, text="人物名称").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(self.setup_card, textvariable=self.character_name, width=40).grid(
            row=2, column=1, sticky="w", padx=(10, 0), pady=(10, 0)
        )

        ttk.Label(self.setup_card, text="别名（可选，逗号/空格/换行分隔）").grid(
            row=3, column=0, sticky="w", pady=(10, 0)
        )
        ttk.Entry(self.setup_card, textvariable=self.aliases, width=60).grid(
            row=3, column=1, sticky="we", padx=(10, 0), pady=(10, 0)
        )

        self.start_btn = ttk.Button(self.setup_card, text="开始生成", command=self._start)
        self.start_btn.grid(row=4, column=1, sticky="w", pady=(14, 0))

        self.setup_card.columnconfigure(1, weight=1)

        # Main area (outputs)
        main = ttk.Frame(outer)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, textvariable=self.status, foreground="#666").pack(anchor="w")

        cards = ttk.Frame(main)
        cards.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        left = ttk.Frame(cards)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right = ttk.Frame(cards)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))

        self.archive_card = ttk.Frame(left, padding=12, relief="solid")
        self.archive_card.pack(fill=tk.BOTH, expand=True)
        ttk.Label(self.archive_card, text="人物档案（自动生成）", foreground="#666").pack(anchor="w")
        self.archive_text = tk.Text(self.archive_card, height=16, wrap="word")
        self.archive_text.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        btns1 = ttk.Frame(self.archive_card)
        btns1.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btns1, text="复制", command=lambda: self._copy_text(self.archive_text)).pack(
            side=tk.LEFT
        )

        self.system_card = ttk.Frame(right, padding=12, relief="solid")
        self.system_card.pack(fill=tk.BOTH, expand=True)
        ttk.Label(self.system_card, text="扮演指令（system prompt）", foreground="#666").pack(anchor="w")
        self.system_text = tk.Text(self.system_card, height=16, wrap="word")
        self.system_text.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        btns2 = ttk.Frame(self.system_card)
        btns2.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btns2, text="复制", command=lambda: self._copy_text(self.system_text)).pack(
            side=tk.LEFT
        )

        bottom = ttk.Frame(main)
        bottom.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(bottom, text="打开输出目录（artifacts）", command=lambda: _open_dir(ARTIFACTS_DIR)).pack(
            side=tk.LEFT
        )

    def _copy_text(self, widget: tk.Text) -> None:
        text = widget.get("1.0", tk.END).strip()
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        messagebox.showinfo("已复制", "已复制到剪贴板。")

    def _pick_novel(self) -> None:
        path = filedialog.askopenfilename(
            title="选择小说文本文件",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.novel_path.set(path)

    def _validate(self) -> tuple[str, str, str, str]:
        key = self.api_key.get().strip()
        if not key:
            raise ValueError("请输入 DeepSeek API Key。")
        novel = self.novel_path.get().strip()
        if not novel or not os.path.exists(novel):
            raise ValueError("请选择有效的小说 txt 文件。")
        name = self.character_name.get().strip()
        if not name:
            raise ValueError("请输入人物名称。")
        aliases = self.aliases.get()
        return key, novel, name, aliases

    def _set_status(self, s: str) -> None:
        self.status.set(s)
        self.update_idletasks()

    def _start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        try:
            key, novel, name, aliases = self._validate()
        except ValueError as e:
            messagebox.showwarning("信息不完整", str(e))
            return

        self.start_btn.configure(state="disabled")
        self.archive_text.delete("1.0", tk.END)
        self.system_text.delete("1.0", tk.END)

        def worker() -> None:
            try:
                result = run_full_pipeline(
                    api_key=key,
                    novel_path=novel,
                    character_name=name,
                    aliases_text=aliases,
                    output_dir=ARTIFACTS_DIR,
                    model="deepseek-reasoner",
                    status_cb=lambda t: self.after(0, self._set_status, t),
                )
                self.after(0, lambda: self._show_result(result.archive_md, result.system_prompt_md))
                self.after(0, lambda: self._set_status(f"完成：已输出到 {ARTIFACTS_DIR}"))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("生成失败", str(e)))
                self.after(0, lambda: self._set_status("失败：请检查网络/API Key/文本格式"))
            finally:
                self.after(0, lambda: self.start_btn.configure(state="normal"))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self._set_status("开始…")

    def _show_result(self, archive_md: str, system_md: str) -> None:
        self.archive_text.insert(tk.END, archive_md)
        self.system_text.insert(tk.END, system_md)


def main() -> None:
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

