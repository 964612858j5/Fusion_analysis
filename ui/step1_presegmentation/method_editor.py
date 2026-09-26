"""The `+` dialog: one method and its parameter lists (plan block B, 4.1).

The method list is the eight UI methods (R2 hides HQ / HQ2 / CDS); Mesmer is
listed but disabled, with the reason, where this environment has no DeepCell
(F1). A list parameter takes comma-separated values, any other exactly one;
every field is checked as it is typed against the parameter table (plan
7.1), and the combination count is shown live. Save stays disabled while any
field is refused.
"""

from PyQt5 import QtWidgets

from ...utils import segmentation_param_schema as ps


class MethodEditorDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, method=None, values=None, lock_method=False,
                 mesmer_available=None):
        super().__init__(parent)
        self.setWindowTitle("Edit method" if lock_method else "Add method")
        self.setMinimumWidth(420)
        self._mesmer_ok = (ps.mesmer_available() if mesmer_available is None
                           else bool(mesmer_available))
        self._fields = {}                 # key -> (spec, QLineEdit, error QLabel)
        self._values = {}

        outer = QtWidgets.QVBoxLayout(self)
        # A plain row, not a second QFormLayout: two forms size their label
        # columns separately and this one's came out zero wide.
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Method:", self))
        self.method_combo = QtWidgets.QComboBox(self)
        for m in ps.UI_METHODS:
            label = ps.display_name(m)
            if m in ps.MESMER_METHODS and not self._mesmer_ok:
                label += "  (DeepCell is not installed)"
            self.method_combo.addItem(label, m)
            if m in ps.MESMER_METHODS and not self._mesmer_ok:
                item = self.method_combo.model().item(self.method_combo.count() - 1)
                item.setEnabled(False)
        top.addWidget(self.method_combo, 1)
        outer.addLayout(top)

        self._form_host = QtWidgets.QWidget(self)
        self._form = QtWidgets.QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._form_host)

        self.count_label = QtWidgets.QLabel("", self)
        self.count_label.setStyleSheet("color:#bbb;")
        outer.addWidget(self.count_label)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel, parent=self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

        start = method if method in ps.UI_METHODS else next(
            m for m in ps.UI_METHODS if self._selectable(m))
        self.method_combo.setCurrentIndex(ps.UI_METHODS.index(start))
        self.method_combo.setEnabled(not lock_method)
        self.method_combo.currentIndexChanged.connect(lambda _i: self._rebuild(None))
        self._rebuild(values)

    # ── state ────────────────────────────────────────────────────────
    def method(self):
        return self.method_combo.currentData()

    def values(self):
        """{key: [values]} for the method -- only meaningful when valid."""
        return {k: list(v) for k, v in self._values.items()}

    def is_valid(self):
        """Every field parsed (a tidy-up note does not count against it)."""
        return len(self._values) == len(self._fields) and self._selectable(self.method())

    def field(self, key):
        return self._fields[key][1]

    def field_error(self, key):
        """Why the field is refused, or "" (a tidy-up note is not a refusal)."""
        return "" if key in self._values else self._fields[key][2].text()

    def field_note(self, key):
        return self._fields[key][2].text() if key in self._values else ""

    # ── internals ────────────────────────────────────────────────────
    def _selectable(self, method):
        return self._mesmer_ok or method not in ps.MESMER_METHODS

    def _rebuild(self, values):
        while self._form.rowCount():
            self._form.removeRow(0)
        self._fields, self._values = {}, {}
        method = self.method()
        start = values or ps.default_values(method)
        for spec in ps.specs(method):
            edit = QtWidgets.QLineEdit(self._form_host)
            edit.setText(ps.format_values(
                spec, [spec.default] if spec.fixed else (start.get(spec.key) or [spec.default])))
            edit.setReadOnly(spec.fixed)
            err = QtWidgets.QLabel("", self._form_host)
            err.setStyleSheet("color:#ff6b6b;font-size:10px;")
            err.setWordWrap(True)
            box = QtWidgets.QWidget(self._form_host)
            col = QtWidgets.QVBoxLayout(box)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(1)
            col.addWidget(edit)
            if spec.note:
                hint = QtWidgets.QLabel(spec.note, box)
                hint.setStyleSheet("color:#888;font-size:10px;")
                col.addWidget(hint)
            col.addWidget(err)
            self._form.addRow(spec.describe() + ":", box)
            self._fields[spec.key] = (spec, edit, err)
            edit.textChanged.connect(lambda _t, k=spec.key: self._check(k))
            self._check(spec.key)
        self._refresh()

    def _check(self, key):
        spec, edit, err = self._fields[key]
        try:
            vals, note = ps.parse_values(spec, edit.text())
        except ps.ParamError as exc:
            self._values.pop(key, None)
            err.setText(str(exc))
            err.setStyleSheet("color:#ff6b6b;font-size:10px;")
        else:
            self._values[key] = vals
            err.setText("")
            if note:
                # A tidy-up, not a refusal: said, but it does not block Save.
                err.setStyleSheet("color:#ffd166;font-size:10px;")
                err.setText(note)
        self._refresh()

    def _refresh(self):
        if not hasattr(self, "buttons"):
            return
        ok = self.is_valid()
        self.buttons.button(QtWidgets.QDialogButtonBox.Save).setEnabled(ok)
        if ok:
            n = ps.combo_count(self.method(), self._values)
            self.count_label.setText(f"{n} combination{'s' if n != 1 else ''}")
        else:
            self.count_label.setText("Fix the fields in red to save.")
