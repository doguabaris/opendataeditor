from pathlib import Path
from frictionless import system

from PySide6.QtCore import Qt, QAbstractTableModel, QObject, Signal, Slot, QRunnable
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTableView, QLabel, QApplication

import pandas as pd

from ode import utils
from ode.file import File

DEFAULT_LIMIT_ERRORS = 999


class DataWorkerSignals(QObject):
    """Define the signals for the DataWorker."""

    finished = Signal(tuple)
    messages = Signal(str)


class DataWorker(QRunnable):
    """Worker to execute all the reading and validation tasks.

    This worker will allow us to read and validate the data (that can take several
    seconds) in the background. By moving this logic to a Worker we can avoid the
    application to get freeze while reading and instead display proper messages to
    the user.
    """

    def __init__(self, filepath):
        super().__init__()
        self.file = File(filepath)
        self.signals = DataWorkerSignals()
        self.resource = self.file.get_or_create_metadata().get("resource")

    @Slot()
    def run(self):
        """Reads and validates the data.

        We are using Resource.read_cells() because we want to read the data
        as is in the file in order to properly show data errors.

        We are using the system context to allow us to read from non-relative paths.

        This method emits a finished signal with all the data that the main UI requires to
        display the table and the errors.
        """
        with system.use_context(trusted=True):
            self.signals.messages.emit(QApplication.translate("DataWorker", "Reading file..."))
            data = self.resource.read_cells()
            self.signals.messages.emit(QApplication.translate("DataWorker", "Validating file..."))
            report = self.resource.validate(limit_errors=DEFAULT_LIMIT_ERRORS)

        errors = []
        if not report.valid:
            try:
                # report.error is only available for single error report
                errors.append(report.error)
            except Exception:
                errors = report.tasks[0].errors

        self.signals.messages.emit(QApplication.translate("DataWorker", "Read and Validation finished."))
        self.signals.finished.emit((self.file.path, data, errors))


class FrictionlessTableModel(QAbstractTableModel):
    def __init__(self, data=[], errors=[]):
        super().__init__()
        self._data = data
        self._row_count = self._get_row_count()
        self.errors = self._get_errors(errors)
        self._column_count = self._get_column_count()

    def write_data(self, filepath: Path):
        df = pd.DataFrame(self._data[1:], columns=self._data[0])
        extension = filepath.suffix.lower()
        if extension == ".csv":
            resource = File(filepath).get_or_create_metadata().get("resource")
            dialect = resource.dialect.to_dict()
            csv = dialect.get("csv", None)
            if csv:
                df.to_csv(filepath, index=False, sep=csv["delimiter"])
            else:
                df.to_csv(filepath, index=False)
            print(f"Data saved in CSV format: {filepath}")
        elif extension in [".xlsx", ".xls"]:
            df.to_excel(filepath, index=False, engine="openpyxl")
            print(f"Data saved in Excel format: {filepath}")
        else:
            raise ValueError(f"Unsupported format: {extension}. Use .csv, .xlsx or .xls")

    def _get_errors(self, errors):
        """Return an array with errors information to use when rendering the table.

        The array has same lenght as our data with None or a Tuple:
          [None, None, (3, 'blank-label', 'error mesage to be displayed'), None]

        Main actions:
         - Moves from Frictionless' 1-index to 0-index
         - Handles inconsistency in Fricionless Error Object API
         - Builds an array for easy access to error information to render performant tables.
        """
        result = [None] * self._row_count
        for error in errors:
            if error.type == "source-error":
                # SourceError happens with files that cannot be read and do not have row_number nor field_number.
                return result
            elif error.type in ["blank-label", "duplicate-label", "incorrect-label", "missing-label", "extra-label"]:
                # https://github.com/frictionlessdata/frictionless-py/issues/1710
                row = error.row_numbers[0] - 1
                column = error.field_number - 1
            elif error.type == "blank-row":
                row = error.row_number - 1
                # BlankRow error does not have field_number
                column = 0
            elif not hasattr(error, "row_number"):
                row = None
                column = None
            else:
                row = error.row_number - 1
                column = error.field_number - 1

            if row is not None:
                if result[row] is None:
                    result[row] = list()

                result[row].append((column, error.type, error.message))

        return result

    def _get_row_count(self):
        return len(self._data)

    def _get_column_count(self):
        """Get the amout of columns.

        We are expecting malformed CSVs, so the amout of columns should always
        be the size of the longest row.
        """
        try:
            return max(map(len, self._data))
        except ValueError:
            return 0

    def get_header_data(self):
        """Returns the first row of the file."""
        return self._data[0]

    def rowCount(self, parent=None):
        """Returning from a pre-calculated private attribute for performance improvements."""
        return self._row_count

    def columnCount(self, parent=None):
        """Returning from a pre-calculated private attribute for performance improvements."""
        return self._column_count

    def data(self, index, role):
        """Returns information to be used to render the Data Table View.

        For each cell we return:
         - The value to be displayed in the cell
         - If there is an error in that cell, a red color for the Background.
         - If there is an error in that cell, a message for the tooltip.
        """
        if not index.isValid():
            return None

        if role == Qt.ItemDataRole.DisplayRole or role == Qt.ItemDataRole.EditRole:
            try:
                value = self._data[index.row()][index.column()]
            except IndexError:
                # Our data could be irregular (missing columns and rows)
                # So it is okay to return None and keep iterating.
                return None
            return value
        if role == Qt.ItemDataRole.BackgroundRole:
            if not self.errors[index.row()]:
                return
            for error_column, error_type, error_message in self.errors[index.row()]:
                if error_type == "blank-row":
                    # BlankRowError does not have field_number, we paint all the cells.
                    return QColor("red")
                if error_column == index.column():
                    return QColor("red")

    def flags(self, index):
        """Enable edition mode"""
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return super().flags(index) | Qt.ItemFlag.ItemIsEditable

    def setData(self, index, value, role):
        """Insert the edited value at the specific cell.

        Since the TableView will always have enough rows and enough columns,
        when editing the raw data we encounter two scenarios:
          a) The cell in the raw data exist and therefore we just replace
          b) The cell in the raw data do not exist and therefore we need to
          create empty cells until we get to the exact column we want to insert.
        """
        if role == Qt.ItemDataRole.EditRole:
            currentRow = self._data[index.row()]
            try:
                # a) raw cell exist
                currentRow[index.column()] = value
            except IndexError:
                # b) we create empty cells and then insert at specific column
                currentRow += [None] * (index.column() - len(currentRow))
                currentRow.insert(index.column(), value)
            return True
        return False


class DataViewer(QWidget):
    """Widget to display the content of tabular data."""

    def __init__(self):
        super().__init__()

        utils.set_common_style(self)

        layout = QVBoxLayout()
        self.setLayout(layout)

        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.table_view = QTableView()
        # TableView's corner button hangs the application when working with huge datasets so we disable it.
        self.table_view.setCornerButtonEnabled(False)
        self.table_view.hide()

        layout.addWidget(self.label)
        layout.addWidget(self.table_view)

        self.retranslateUI()

    def display_data(self, model):
        """Set the model of the QTableView

        When a tabular file is selected, the main application will create a
        FrictionlessTableModel and call this function using the model as a parametner.
        """
        self.table_view.setModel(model)
        self.label.hide()
        self.table_view.show()

    def clear(self, model):
        """Reset the view to the default state.

        This view depends of the main application self.table_model attribute. This
        method should always receive an empty model
        """
        self.table_view.setModel(model)
        self.label.show()
        self.table_view.hide()

    def retranslateUI(self):
        """Apply translations to class elements."""
        self.label.setText(self.tr("Preview not available for this item."))
