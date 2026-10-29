"""wxPython desktop interface. Network and disk work runs outside the UI thread."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from uuid import uuid4

import wx

from . import config
from .models import Connection, Entry, FIELDS, Preferences, PROVIDERS
from .providers import connect
from .transfers import Location, TransferQueue, copy_tree, publish_file, rename_remote

DRAG_FORMAT = "application/x-bucket-browser-items"


def load_icon_bundle(path):
    """Build native window icons from the supplied application artwork."""
    image = wx.Image(str(path), wx.BITMAP_TYPE_PNG)
    if not image.IsOk():
        raise ValueError(f"Unable to load application icon: {path}")
    icons = wx.IconBundle()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        bitmap = wx.Bitmap(image.Scale(size, size, wx.IMAGE_QUALITY_HIGH))
        icon = wx.Icon()
        icon.CopyFromBitmap(bitmap)
        icons.AddIcon(icon)
    return icons


def button(parent, label, handler):
    control = wx.Button(parent, label=label, size=(-1, 38))
    control.Bind(wx.EVT_BUTTON, handler)
    return control


def message(parent, text, title="Bucket Browser", style=wx.OK | wx.ICON_INFORMATION):
    return wx.MessageBox(text, title, style, parent)


def human_size(size):
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if size < 1024 or unit == "TiB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024


class ConnectionEditor(wx.Dialog):
    def __init__(self, parent, connection=None):
        super().__init__(parent, title="Connection details", size=(640, 640), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.original = connection
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)
        self.name = wx.TextCtrl(panel, value=connection.name if connection else "")
        self.provider = wx.Choice(panel, choices=list(PROVIDERS.values()))
        self.provider.SetSelection(list(PROVIDERS).index(connection.provider) if connection else 0)
        self.bucket = wx.TextCtrl(panel, value=connection.bucket if connection else "")
        for title, control in [("Connection name", self.name), ("Provider", self.provider),
                               ("Bucket / container ID (optional; blank lists available buckets)", self.bucket)]:
            outer.Add(wx.StaticText(panel, label=title), 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
            outer.Add(control, 0, wx.EXPAND | wx.ALL, 8)
        self.fields_panel = wx.Panel(panel)
        self.fields_sizer = wx.BoxSizer(wx.VERTICAL)
        self.fields_panel.SetSizer(self.fields_sizer)
        outer.Add(self.fields_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.show_secrets = wx.CheckBox(panel, label="Show credentials")
        self.show_secrets.Bind(wx.EVT_CHECKBOX, self.toggle_secrets)
        outer.Add(self.show_secrets, 0, wx.ALL, 10)
        outer.Add(wx.StaticText(panel, label="Azure: use a complete connection string OR all three account fields.\n"
                  "Connections are saved after a successful bucket listing."), 0, wx.ALL, 10)
        controls = wx.BoxSizer(wx.HORIZONTAL)
        controls.AddStretchSpacer()
        controls.Add(button(panel, "Connect && save", self.accept), 0, wx.ALL, 5)
        controls.Add(wx.Button(panel, wx.ID_CANCEL, size=(-1, 38)), 0, wx.ALL, 5)
        outer.Add(controls, 0, wx.EXPAND | wx.ALL, 5)
        panel.SetSizer(outer)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(panel, 1, wx.EXPAND)
        self.SetSizer(layout)
        self.provider.Bind(wx.EVT_CHOICE, self.build_fields)
        self.build_fields()
        parent.frame.apply_theme(self)
        self.CentreOnParent()

    def build_fields(self, event=None):
        self.fields_sizer.Clear(True)
        provider = list(PROVIDERS)[self.provider.GetSelection()]
        settings = self.original.settings if self.original and self.original.provider == provider else {}
        self.fields = {}
        for key, title, secret in FIELDS[provider]:
            self.fields_sizer.Add(wx.StaticText(self.fields_panel, label=title), 0, wx.TOP, 5)
            value = settings.get(key, "core.windows.net" if key == "endpoint_suffix" else "")
            control = wx.TextCtrl(self.fields_panel, value=value, style=wx.TE_PASSWORD if secret and not self.show_secrets.GetValue() else 0)
            self.fields[key] = control
            self.fields_sizer.Add(control, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)
        self.Layout()
        self.GetParent().frame.apply_theme(self.fields_panel)

    def toggle_secrets(self, event):
        values = {key: field.GetValue() for key, field in self.fields.items()}
        self.build_fields()
        for key, value in values.items():
            self.fields[key].SetValue(value)

    def accept(self, event):
        self.result = Connection(self.name.GetValue().strip(), list(PROVIDERS)[self.provider.GetSelection()],
                                 {key: field.GetValue().strip() for key, field in self.fields.items()}, self.bucket.GetValue().strip())
        if self.original:
            self.result.id = self.original.id
        try:
            self.result.validate()
        except ValueError as exc:
            message(self, str(exc), style=wx.OK | wx.ICON_ERROR)
            return
        self.EndModal(wx.ID_OK)


class Manager(wx.Dialog):
    def __init__(self, frame):
        super().__init__(frame, title="Connection manager & preferences", size=(810, 630), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.frame = frame
        panel = wx.Panel(self)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.list = wx.ListBox(panel)
        layout.Add(wx.StaticText(panel, label="Successful connections  •  credentials encrypted for your OS account"), 0, wx.ALL, 12)
        layout.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in [("New…", self.new), ("Edit…", self.edit), ("Connect", self.open), ("Delete", self.delete),
                               ("Startup default", self.default)]:
            row.Add(button(panel, label, handler), 0, wx.ALL, 4)
        layout.Add(row, 0, wx.ALL, 8)
        self.default_label = wx.StaticText(panel)
        layout.Add(self.default_label, 0, wx.LEFT | wx.BOTTOM, 12)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(panel, label="Appearance: "), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.theme = wx.Choice(panel, choices=["Dark", "Light"])
        self.theme.SetSelection(0 if frame.preferences.theme == "dark" else 1)
        row.Add(self.theme, 0, wx.ALL, 5)
        self.touch = wx.CheckBox(panel, label="Larger touch targets")
        self.touch.SetValue(frame.preferences.touch)
        row.Add(self.touch, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 8)
        row.Add(button(panel, "Apply preferences", self.apply), 0, wx.ALL, 5)
        layout.Add(row, 0, wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(button(panel, "Import XML…", self.import_xml), 0, wx.ALL, 5)
        row.Add(button(panel, "Export XML…", self.export_xml), 0, wx.ALL, 5)
        row.Add(button(panel, "Clear startup default", self.clear_default), 0, wx.ALL, 5)
        row.AddStretchSpacer()
        row.Add(wx.Button(panel, wx.ID_CLOSE, size=(-1, 38)), 0, wx.ALL, 5)
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 8)
        panel.SetSizer(layout)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
        self.list.Bind(wx.EVT_LISTBOX_DCLICK, self.open)
        self.refresh()
        frame.apply_theme(self)
        self.CentreOnParent()

    def refresh(self):
        self.list.Set([f"{c.name}  —  {PROVIDERS[c.provider]}  —  {c.bucket or 'Choose bucket on connect'}" for c in self.frame.connections])
        default = next((c.name for c in self.frame.connections if c.id == self.frame.preferences.default_connection), "None")
        self.default_label.SetLabel(f"Startup: {default}  {self.frame.preferences.default_bucket}")

    def selected(self):
        index = self.list.GetSelection()
        return self.frame.connections[index] if index != wx.NOT_FOUND else None

    def new(self, event):
        self.edit_connection(None)

    def edit(self, event):
        if self.selected():
            self.edit_connection(self.selected())

    def edit_connection(self, connection):
        with ConnectionEditor(self, connection) as editor:
            if editor.ShowModal() == wx.ID_OK:
                candidate = editor.result
            else:
                return
        self.EndModal(wx.ID_OK)
        self.frame.open_connection(candidate)

    def open(self, event):
        connection = self.selected()
        if connection:
            self.EndModal(wx.ID_OK)
            self.frame.open_connection(copy.deepcopy(connection))

    def delete(self, event):
        connection = self.selected()
        if connection and message(self, f"Remove saved connection '{connection.name}'? Open tabs stay connected.", style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) == wx.YES:
            self.frame.connections.remove(connection)
            if self.frame.preferences.default_connection == connection.id:
                self.frame.preferences.default_connection = self.frame.preferences.default_bucket = ""
            self.frame.persist()
            self.refresh()

    def default(self, event):
        connection = self.selected()
        if not connection:
            return
        bucket = connection.bucket
        if not bucket:
            with wx.TextEntryDialog(self, "Bucket / container ID to open automatically:", "Startup bucket") as dialog:
                if dialog.ShowModal() != wx.ID_OK or not dialog.GetValue().strip():
                    return
                bucket = dialog.GetValue().strip()
        self.frame.preferences.default_connection = connection.id
        self.frame.preferences.default_bucket = bucket
        self.frame.persist()
        self.refresh()

    def clear_default(self, event):
        self.frame.preferences.default_connection = self.frame.preferences.default_bucket = ""
        self.frame.persist()
        self.refresh()

    def apply(self, event):
        self.frame.preferences.theme = "dark" if self.theme.GetSelection() == 0 else "light"
        self.frame.preferences.touch = self.touch.GetValue()
        self.frame.persist()
        self.frame.apply_theme(self.frame)
        self.frame.apply_theme(self)

    def import_xml(self, event):
        with wx.FileDialog(self, "Import Bucket Browser XML", wildcard="XML files (*.xml)|*.xml", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            try:
                preferences, connections = config.load(dialog.GetPath())
                if message(self, "Replace saved connections and preferences with this XML? Open tabs stay connected.", style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) != wx.YES:
                    return
                # Write before replacing the current in-memory state.
                config.save(self.frame.settings_path, preferences, connections)
                self.frame.preferences, self.frame.connections = preferences, connections
                self.frame.load_error = None
                self.frame.remember_secrets(connections)
                self.theme.SetSelection(0 if preferences.theme == "dark" else 1)
                self.touch.SetValue(preferences.touch)
                self.refresh()
                self.frame.apply_theme(self.frame)
                self.frame.apply_theme(self)
            except Exception as exc:
                self.frame.error(exc)

    def export_xml(self, event):
        with wx.SingleChoiceDialog(self, "Choose how credentials are exported. Portable XML contains readable secrets.", "Export XML", ["Settings only (omit credentials)", "Encrypted for this OS account", "Portable XML with plaintext credentials"]) as choice:
            if choice.ShowModal() != wx.ID_OK:
                return
            mode = ["redacted", "encrypted", "portable"][choice.GetSelection()]
        with wx.FileDialog(self, "Export Bucket Browser XML", defaultFile="bucket-browser.xml", wildcard="XML files (*.xml)|*.xml", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                try:
                    if Path(dialog.GetPath()).resolve() == self.frame.settings_path.resolve() and mode != "encrypted":
                        raise ValueError("Choose another path for a portable or redacted export.")
                    config.save(dialog.GetPath(), self.frame.preferences, self.frame.connections, mode)
                except Exception as exc:
                    self.frame.error(exc)


class FileTarget(wx.DropTarget):
    def __init__(self, pane):
        super().__init__()
        self.pane = pane
        self.data = wx.DataObjectComposite()
        self.custom = wx.CustomDataObject(DRAG_FORMAT)
        self.files = wx.FileDataObject()
        self.data.Add(self.custom, True)
        self.data.Add(self.files)
        self.SetDataObject(self.data)

    def OnData(self, x, y, result):
        if not self.GetData():
            return wx.DragNone
        try:
            received = self.data.GetReceivedFormat()
            if received.GetId() == DRAG_FORMAT:
                token = bytes(self.custom.GetData()).decode()
                source, entries = self.pane.frame.drags[token]
            else:
                source = Location()
                entries = [local_entry(Path(p)) for p in self.files.GetFilenames()]
            destination = self.pane.location()
            index, flags = self.pane.list.HitTest((x, y))
            if index >= 0 and self.pane.entries[index].is_dir:
                folder = self.pane.entries[index]
                if destination.provider is None:
                    destination.local = folder.key
                else:
                    destination.prefix = folder.key
            self.pane.frame.enqueue_copy(source, entries, destination)
            return wx.DragCopy
        except Exception as exc:
            self.pane.frame.error(exc)
            return wx.DragNone


def local_entry(path):
    info = path.stat()
    return Entry(str(path), path.name, info.st_size if path.is_file() else 0,
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(info.st_mtime)), path.is_dir())


class BrowserPane(wx.Panel):
    def __init__(self, parent, frame, title, provider=None, bucket="", connection=None, initial=None):
        super().__init__(parent)
        self.frame, self.provider, self.bucket, self.connection = frame, provider, bucket, connection
        self.path = "" if provider else str(Path(initial or Path.home()).resolve())
        self.entries, self.generation = [], 0
        self.busy = False
        outer = wx.BoxSizer(wx.VERTICAL)
        heading = wx.BoxSizer(wx.HORIZONTAL)
        heading.Add(wx.StaticText(self, label=title), 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 8)
        heading.Add(button(self, "↑ Up", self.up), 0, wx.ALL, 3)
        heading.Add(button(self, "Refresh", lambda e: self.refresh()), 0, wx.ALL, 3)
        self.close_button = button(self, "×", self.close_tab)
        self.close_button.SetToolTip("Close this tab")
        heading.Add(self.close_button, 0, wx.ALL, 3)
        outer.Add(heading, 0, wx.EXPAND)
        self.address = wx.TextCtrl(self, value=self.path, style=wx.TE_PROCESS_ENTER)
        self.address.SetHint("Bucket root" if provider else "Local folder")
        self.address.Bind(wx.EVT_TEXT_ENTER, self.navigate)
        if provider and provider.flat:
            self.address.ChangeValue("Appwrite bucket • flat file list")
            self.address.SetEditable(False)
        outer.Add(self.address, 0, wx.EXPAND | wx.ALL, 5)
        actions = wx.BoxSizer(wx.HORIZONTAL)
        actions.Add(button(self, "Copy to…", self.copy_to), 0, wx.ALL, 3)
        actions.Add(button(self, "Rename", self.rename), 0, wx.ALL, 3)
        actions.Add(button(self, "Delete", self.delete), 0, wx.ALL, 3)
        outer.Add(actions, 0, wx.EXPAND)
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
        for index, (label, width) in enumerate([("Name", 245), ("Size", 95), ("Modified", 160)]):
            self.list.InsertColumn(index, label, width=width)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.activate)
        self.list.Bind(wx.EVT_LIST_BEGIN_DRAG, self.drag)
        self.list.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self.context)
        self.list.Bind(wx.EVT_KEY_DOWN, self.key_down)
        self.list.SetDropTarget(FileTarget(self))
        outer.Add(self.list, 1, wx.EXPAND | wx.ALL, 5)
        self.info = wx.StaticText(self, label="Loading…")
        outer.Add(self.info, 0, wx.ALL, 8)
        self.SetSizer(outer)
        self.set_density()
        frame.apply_theme(self)
        self.refresh()

    def set_density(self):
        size = 32 if self.frame.preferences.touch else 20
        self.images = wx.ImageList(size, size)
        self.images.Add(wx.ArtProvider.GetBitmap(wx.ART_FOLDER, wx.ART_OTHER, (size, size)))
        self.images.Add(wx.ArtProvider.GetBitmap(wx.ART_NORMAL_FILE, wx.ART_OTHER, (size, size)))
        self.list.AssignImageList(self.images, wx.IMAGE_LIST_SMALL)

    def location(self):
        return Location(self.provider, self.bucket, self.path if self.provider else "", self.path if not self.provider else "")

    def selected(self):
        selected, index = [], self.list.GetFirstSelected()
        while index != -1:
            if index < len(self.entries):
                selected.append(self.entries[index])
            index = self.list.GetNextSelected(index)
        return selected

    def refresh(self):
        self.generation += 1
        generation, path = self.generation, self.path
        self.busy = True
        self.info.SetLabel("Loading…")
        def work():
            if self.provider:
                return self.provider.list(self.bucket, path)
            result = []
            for p in Path(path).iterdir():
                try:
                    result.append(local_entry(p))
                except (PermissionError, FileNotFoundError):
                    continue
            return sorted(result, key=lambda e: (not e.is_dir, e.name.casefold()))
        def done(entries):
            if not self or generation != self.generation:
                return
            self.busy = False
            self.entries = entries
            self.list.Freeze()
            self.list.DeleteAllItems()
            for entry in entries:
                index = self.list.InsertItem(self.list.GetItemCount(), entry.name, 0 if entry.is_dir else 1)
                self.list.SetItem(index, 1, "Folder" if entry.is_dir else human_size(entry.size))
                self.list.SetItem(index, 2, entry.modified)
            self.list.Thaw()
            self.info.SetLabel(f"{len(entries)} items  •  Drop files here to copy" if self.provider else f"{len(entries)} items")
        def failed(exc):
            if self and generation == self.generation:
                self.busy = False
                self.info.SetLabel("Listing failed • use Refresh to retry")
                self.frame.error(exc)
        self.frame.background(work, done, failed)

    def go(self, path):
        self.path = path
        self.address.ChangeValue(path)
        if not self.provider:
            self.frame.preferences.local_path = path
            book = self.GetParent()
            index = book.FindPage(self)
            if index != wx.NOT_FOUND:
                book.SetPageText(index, self.frame.local_tab_name(path))
                book.SetToolTip(path)
        self.refresh()

    def navigate(self, event):
        if self.provider:
            path = self.address.GetValue().strip().strip("/")
            self.go(path + "/" if path else "")
        else:
            path = Path(os.path.expandvars(self.address.GetValue())).expanduser()
            if path.is_dir():
                self.go(str(path.resolve()))
            else:
                message(self, "Choose an existing local directory.")

    def up(self, event):
        if self.provider:
            if not self.provider.flat:
                parent = self.path.rstrip("/").rsplit("/", 1)[0] if "/" in self.path.rstrip("/") else ""
                self.go(parent + "/" if parent else "")
        else:
            self.go(str(Path(self.path).parent))

    def activate(self, event):
        if self.entries[event.GetIndex()].is_dir:
            self.go(self.entries[event.GetIndex()].key)

    def context(self, event):
        menu = wx.Menu()
        for label, handler in [("Copy selected to…", self.copy_to), ("Rename…", self.rename), ("Delete…", self.delete), ("Refresh", lambda e: self.refresh())]:
            item = menu.Append(wx.ID_ANY, label)
            menu.Bind(wx.EVT_MENU, handler, item)
        self.PopupMenu(menu)
        menu.Destroy()

    def key_down(self, event):
        if event.GetKeyCode() == wx.WXK_F5:
            self.refresh()
        elif event.GetKeyCode() == wx.WXK_DELETE:
            self.delete(event)
        elif event.GetKeyCode() == wx.WXK_F2:
            self.rename(event)
        elif event.GetKeyCode() == ord("A") and event.CmdDown():
            for i in range(len(self.entries)):
                self.list.SetItemState(i, wx.LIST_STATE_SELECTED, wx.LIST_STATE_SELECTED)
        else:
            event.Skip()

    def copy_to(self, event):
        entries = self.selected()
        panes = [p for p in self.frame.panes() if p is not self]
        if not entries or not panes:
            message(self, "Select files and open a destination folder or bucket first.")
            return
        labels = [p.display_name() for p in panes]
        with wx.SingleChoiceDialog(self, "Destination folder", "Copy selected items", labels) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.frame.enqueue_copy(self.location(), entries, panes[dialog.GetSelection()].location())

    def display_name(self):
        return (self.connection.name + " • " if self.connection else "Local • ") + self.location().label

    def rename(self, event):
        selected = self.selected()
        if len(selected) != 1 or selected[0].is_dir:
            message(self, "Select one file to rename. Folder rename is not supported.")
            return
        entry = selected[0]
        with wx.TextEntryDialog(self, "New filename:", "Rename file", entry.name) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name = dialog.GetValue().strip()
        if name == entry.name:
            return
        location = self.location()
        def work(progress):
            if location.provider:
                rename_remote(location, entry, name, progress)
            else:
                from .transfers import safe_destination
                destination = safe_destination(location.local, name)
                if destination.parent != Path(location.local).resolve():
                    raise ValueError("Enter a filename without folder separators.")
                if destination.exists():
                    raise FileExistsError("Destination already exists")
                publish_file(entry.key, destination)
        self.frame.transfers.submit("Rename " + entry.name, work)

    def delete(self, event):
        selected = self.selected()
        if not selected:
            return
        if any(e.is_dir for e in selected):
            message(self, "Select files only. Folder deletion is not supported; open the folder to delete its files.")
            return
        if message(self, f"Permanently delete {len(selected)} selected file(s)?", "Delete files", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) != wx.YES:
            return
        location = self.location()
        for entry in selected:
            def work(progress, entry=entry):
                progress(0)
                if location.provider:
                    location.provider.delete(location.bucket, entry)
                else:
                    Path(entry.key).unlink()
            self.frame.transfers.submit("Delete " + entry.name, work)

    def drag(self, event):
        entries = self.selected()
        if not entries:
            return
        location = self.location()
        token = uuid4().hex
        self.frame.drags[token] = (location, entries)
        def start(paths):
            if not self:
                return
            data = wx.DataObjectComposite()
            custom = wx.CustomDataObject(DRAG_FORMAT)
            custom.SetData(token.encode())
            data.Add(custom, True)
            files = wx.FileDataObject()
            for path in paths:
                files.AddFile(str(path))
            data.Add(files)
            source = wx.DropSource(self.list)
            source.SetData(data)
            self.frame.SetStatusText("Drag prepared • drop onto a bucket, a local folder, or your file manager")
            source.DoDragDrop(wx.Drag_CopyOnly)
            self.frame.drags.pop(token, None)
        if not self.provider:
            start([entry.key for entry in entries])
        else:
            # File managers need real paths. Stage asynchronously before starting the OS drag.
            folder = self.frame.drag_root / token
            folder.mkdir()
            self.frame.SetStatusText("Preparing cloud files for drag… keep the mouse button held")
            def stage():
                self.frame.copy_items(location, entries, Location(local=str(folder)), lambda n=0: None)
                return list(folder.iterdir())
            def failed(exc):
                self.frame.drags.pop(token, None)
                self.frame.error(exc)
            self.frame.background(stage, start, failed)

    def close_tab(self, event):
        book = self.GetParent()
        if not self.provider and book.GetPageCount() == 1:
            return
        index = book.FindPage(self)
        if index != wx.NOT_FOUND:
            book.DeletePage(index)
            if not self.provider:
                self.frame.update_local_tabs()


class MainFrame(wx.Frame):
    def __init__(self, icon_path=None):
        super().__init__(None, title="Bucket Browser • BB", size=(1540, 900))
        self.SetIcons(load_icon_bundle(icon_path or Path(__file__).resolve().parents[1] / "bucket.png"))
        self.SetMinSize((1000, 650))
        self.settings_path = config.default_path()
        self.preferences, self.connections = Preferences(), []
        self.load_error = None
        try:
            if self.settings_path.exists():
                self.preferences, self.connections = config.load(self.settings_path)
        except Exception as exc:
            self.load_error = exc
        self.executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="BB browser")
        self.closing = False
        self.drags = {}
        self.secret_values = set()
        self.remember_secrets(self.connections)
        self.drag_root = Path(tempfile.mkdtemp(prefix="bb-drags-"))
        self.queue_rows = {}
        self.pending_updates = {}
        import threading
        self.update_lock = threading.Lock()
        self.transfers = TransferQueue(self.queue_changed, lambda job: wx.CallAfter(self.job_completed, job))
        self.CreateStatusBar()
        self.SetStatusText("Ready • open the connection manager to connect")
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        toolbar = wx.BoxSizer(wx.HORIZONTAL)
        brand = wx.StaticText(panel, label="BB  |  Bucket Browser")
        brand.SetFont(wx.Font(15, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        toolbar.Add(brand, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 12)
        toolbar.Add(button(panel, "Local tab…", self.new_local_tab), 0, wx.ALL, 5)
        toolbar.Add(button(panel, "Connections && preferences…", self.manager), 0, wx.ALL, 5)
        toolbar.Add(wx.StaticText(panel, label="Open in:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.target_group = wx.Choice(panel, choices=["Cloud A", "Cloud B"])
        self.target_group.SetSelection(0)
        toolbar.Add(self.target_group, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        toolbar.Add(button(panel, "Open bucket…", self.open_saved), 0, wx.ALL, 5)
        root.Add(toolbar, 0, wx.EXPAND)
        self.vertical = wx.SplitterWindow(panel, style=wx.SP_LIVE_UPDATE)
        self.horizontal = wx.Panel(self.vertical)
        columns = wx.GridSizer(rows=1, cols=3, vgap=0, hgap=4)
        self.local_book = wx.Notebook(self.horizontal)
        self.books = [wx.Notebook(self.horizontal), wx.Notebook(self.horizontal)]
        for book in [self.local_book, *self.books]:
            book.SetMinSize((0, 0))
            columns.Add(book, 1, wx.EXPAND)
        self.horizontal.SetSizer(columns)
        self.local_book.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.local_tab_changed)
        path = self.preferences.local_path if Path(self.preferences.local_path).is_dir() else str(Path.home())
        self.add_local_tab(path)
        queue_panel = wx.Panel(self.vertical)
        queue_layout = wx.BoxSizer(wx.VERTICAL)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(queue_panel, label="TRANSFER QUEUE  •  copies preserve source files and existing destinations"), 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 8)
        row.Add(button(queue_panel, "Cancel selected", self.cancel_selected), 0, wx.ALL, 3)
        row.Add(button(queue_panel, "Details", self.queue_details), 0, wx.ALL, 3)
        queue_layout.Add(row, 0, wx.EXPAND)
        self.queue_list = wx.ListCtrl(queue_panel, style=wx.LC_REPORT)
        for index, (label, width) in enumerate([("Operation", 620), ("State", 110), ("Bytes processed", 150)]):
            self.queue_list.InsertColumn(index, label, width=width)
        self.queue_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.queue_details)
        queue_layout.Add(self.queue_list, 1, wx.EXPAND | wx.ALL, 5)
        queue_panel.SetSizer(queue_layout)
        self.vertical.SplitHorizontally(self.horizontal, queue_panel, -200)
        self.vertical.SetSashGravity(1.0)
        self.vertical.SetMinimumPaneSize(125)
        root.Add(self.vertical, 1, wx.EXPAND)
        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.flush_queue, self.timer)
        self.timer.Start(150)
        self.Bind(wx.EVT_CLOSE, self.close)
        new_local_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, self.new_local_tab, id=new_local_id)
        self.SetAcceleratorTable(wx.AcceleratorTable([(wx.ACCEL_CMD, ord("T"), new_local_id)]))
        self.apply_theme(self)
        wx.CallAfter(self.startup)

    def startup(self):
        if self.load_error:
            self.error(RuntimeError("Could not load settings. The original XML has been preserved. " + str(self.load_error)))
            return
        connection = next((c for c in self.connections if c.id == self.preferences.default_connection), None)
        if connection:
            self.open_connection(copy.deepcopy(connection), self.preferences.default_bucket)

    def error(self, exc):
        if self.closing:
            return
        # SDK exceptions can contain signed URLs or credentials; remove configured secret strings.
        text = str(exc)
        for value in sorted(self.secret_values, key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        import re
        text = re.sub(r"(https?://[^\s?'\"]+)\?[^\s'\"]+", r"\1?[redacted]", text)
        message(self, text, "Bucket Browser — action failed", wx.OK | wx.ICON_ERROR)

    def remember_secrets(self, connections):
        from .models import SECRETS
        for connection in connections:
            self.secret_values.update(value for key, value in connection.settings.items() if key in SECRETS and value)

    def background(self, work, success, failure=None):
        if self.closing:
            return
        future = self.executor.submit(work)
        def complete(future):
            try:
                result = future.result()
            except Exception as exc:
                wx.CallAfter(deliver, False, exc)
            else:
                wx.CallAfter(deliver, True, result)
        def deliver(ok, value):
            if not self.closing:
                (success if ok else failure or self.error)(value)
        future.add_done_callback(complete)

    def persist(self):
        if self.load_error:
            self.error(RuntimeError("Settings were not saved because loading failed. Import a valid XML to recover, or restore the settings file."))
            return
        try:
            config.save(self.settings_path, self.preferences, self.connections)
        except Exception as exc:
            self.error(exc)

    def manager(self, event=None):
        with Manager(self) as dialog:
            dialog.ShowModal()

    def open_saved(self, event):
        if not self.connections:
            self.manager()
            return
        with wx.SingleChoiceDialog(self, "Select a connection", "Open bucket", [c.name for c in self.connections]) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.open_connection(copy.deepcopy(self.connections[dialog.GetSelection()]))

    def open_connection(self, connection, startup_bucket=""):
        self.remember_secrets([connection])
        group = self.target_group.GetSelection()
        self.SetStatusText("Connecting to " + connection.name + "…")
        bucket = startup_bucket or connection.bucket
        def work():
            provider = connect(connection)
            if bucket:
                provider.check_bucket(bucket)
                return provider, [(bucket, bucket)]
            return provider, provider.buckets()
        def done(result):
            provider, buckets = result
            selected = bucket
            if not buckets:
                self.SetStatusText("Connected • no buckets returned")
                message(self, "The connection succeeded but no buckets were returned. Check bucket access.")
                return
            if not selected:
                with wx.SingleChoiceDialog(self, "Select a bucket / container", connection.name,
                                          [f"{label} ({key})" if key != label else key for key, label in buckets]) as dialog:
                    if dialog.ShowModal() != wx.ID_OK:
                        self.SetStatusText("Bucket selection cancelled")
                        return
                    selected = buckets[dialog.GetSelection()][0]
                # List permission may exist without file-read permission; verify the selected bucket too.
                self.background(lambda: provider.check_bucket(selected), lambda ignored: finish(provider, selected))
            else:
                finish(provider, selected)
        def finish(provider, selected):
            self.connections = [c for c in self.connections if c.id != connection.id] + [connection]
            self.persist()
            book = self.books[group]
            pane = BrowserPane(book, self, f"{connection.name} • {selected}", provider, selected, copy.deepcopy(connection))
            book.AddPage(pane, f"{connection.name}: {selected}", True)
            self.apply_theme(pane)
            self.SetStatusText("Connected to " + selected)
        self.background(work, done)

    @property
    def local(self):
        """The active local browser, matching the active page in the cloud groups."""
        return self.local_book.GetCurrentPage()

    @staticmethod
    def local_tab_name(path):
        return Path(path).name or Path(path).anchor or path

    def add_local_tab(self, path):
        pane = BrowserPane(self.local_book, self, "LOCAL COMPUTER", initial=path)
        self.local_book.AddPage(pane, self.local_tab_name(pane.path), True)
        self.update_local_tabs()
        self.apply_theme(pane)
        return pane

    def new_local_tab(self, event=None):
        with wx.DirDialog(self, "Open a folder in a new local tab", defaultPath=self.local.path,
                          style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.add_local_tab(dialog.GetPath())

    def local_tab_changed(self, event):
        self.update_local_tabs()
        event.Skip()

    def update_local_tabs(self):
        count = self.local_book.GetPageCount()
        for index in range(count):
            self.local_book.GetPage(index).close_button.Enable(count > 1)
        if self.local:
            self.preferences.local_path = self.local.path
            self.local_book.SetToolTip(self.local.path)

    def panes(self):
        books = ([self.local_book] if hasattr(self, "local_book") else []) + getattr(self, "books", [])
        return [book.GetPage(i) for book in books for i in range(book.GetPageCount())]

    def copy_items(self, source, entries, destination, progress):
        copy_tree(source, entries, destination, progress)

    def enqueue_copy(self, source, entries, destination):
        if not entries:
            return
        # All locations and entries are snapshots, so navigation cannot redirect queued transfers.
        for entry in entries:
            self.transfers.submit(f"Copy {entry.name} → {destination.label}",
                                  lambda progress, entry=entry: self.copy_items(source, [entry], destination, progress))

    def queue_changed(self, job):
        with self.update_lock:
            self.pending_updates[job.id] = job

    def flush_queue(self, event=None):
        if self.closing:
            return
        with self.update_lock:
            updates = list(self.pending_updates.values())
            self.pending_updates.clear()
        for job in updates:
            if job.id not in self.queue_rows:
                self.queue_rows[job.id] = self.queue_list.InsertItem(self.queue_list.GetItemCount(), job.label)
            row = self.queue_rows[job.id]
            self.queue_list.SetItem(row, 1, job.state)
            self.queue_list.SetItem(row, 2, human_size(job.done))

    def job_completed(self, job):
        if self.closing:
            return
        self.SetStatusText(job.state + ": " + job.label + (" • double-click queue row for details" if job.error else ""))
        for pane in self.panes():
            pane.refresh()

    def selected_jobs(self):
        index = self.queue_list.GetFirstSelected()
        rows = set()
        while index != -1:
            rows.add(index)
            index = self.queue_list.GetNextSelected(index)
        return [job for job in self.transfers.jobs if self.queue_rows.get(job.id) in rows]

    def cancel_selected(self, event):
        for job in self.selected_jobs():
            if job.state in {"Queued", "Running"}:
                job.cancel.set()
        self.SetStatusText("Cancellation requested • active network requests finish before stopping")

    def queue_details(self, event):
        for job in self.selected_jobs():
            if job.error:
                self.error(RuntimeError(job.error))
            else:
                message(self, f"{job.label}\n{job.state}\n{human_size(job.done)} processed")

    def apply_theme(self, window):
        dark = self.preferences.theme == "dark"
        if window is self and hasattr(wx.App, "SetAppearance"):
            wx.GetApp().SetAppearance(wx.App.Appearance.Dark if dark else wx.App.Appearance.Light)
        bg, fg, field = (("#20252d", "#e7ecf3", "#2b323d") if dark else ("#f2f5f9", "#1b2738", "#ffffff"))
        font = wx.Font(11 if self.preferences.touch else 10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        def walk(control):
            control.SetBackgroundColour(field if isinstance(control, (wx.ListCtrl, wx.TextCtrl, wx.ListBox)) else bg)
            control.SetForegroundColour(fg)
            if not isinstance(control, wx.Frame):
                control.SetFont(font)
            for child in control.GetChildren():
                walk(child)
            control.Refresh()
        walk(window)
        for pane in self.panes():
            pane.set_density()
        window.Layout()

    def close(self, event):
        active = any(job.state in {"Queued", "Running"} for job in self.transfers.jobs)
        if active:
            message(self, "Transfers are still queued or running. Cancel them and wait for the queue to stop before closing.")
            if event.CanVeto():
                event.Veto()
                return
        self.persist()
        self.closing = True
        self.timer.Stop()
        self.transfers.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        # Active staging tasks may still own files; cleanup is best effort on Windows.
        shutil.rmtree(self.drag_root, ignore_errors=True)
        self.Destroy()


def main(icon_path=None):
    app = wx.App(False)
    if hasattr(wx.App, "SetAppearance"):
        app.SetAppearance(wx.App.Appearance.Dark)
    elif hasattr(wx.App, "MSWEnableDarkMode"):
        app.MSWEnableDarkMode(wx.App.DarkMode_Always)
    frame = MainFrame(icon_path=icon_path)
    frame.Show()
    app.MainLoop()
