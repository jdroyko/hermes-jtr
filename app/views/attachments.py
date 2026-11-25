import os
import uuid

from flask import Blueprint, abort, current_app, flash, redirect, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from ..db import audit, get_db
from ._util import can_edit, get_doc, me

bp = Blueprint("attachments", __name__, url_prefix="/documents")

_SIGNATURES = {
    ".pdf": (b"%PDF",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG",),
}


def _sniff_ok(ext, head):
    return any(head.startswith(sig) for sig in _SIGNATURES.get(ext, ()))


def _dest_dir(doc_id):
    return os.path.join(current_app.config["UPLOAD_FOLDER"], str(doc_id))


@bp.route("/<int:doc_id>/expenses/<int:expense_id>/attachments", methods=["POST"])
def upload_attachment(doc_id, expense_id):
    doc = get_doc(doc_id)
    if not can_edit(doc):
        abort(403)
    db = get_db()
    exp = db.execute("SELECT id FROM expenses WHERE id=? AND document_id=?", (expense_id, doc_id)).fetchone()
    if exp is None:
        abort(404)

    f = request.files.get("receipt")
    if not f or not f.filename:
        flash("Choose a file to upload.", "error")
        return redirect(url_for("documents.expenses", doc_id=doc_id))

    safe_name = secure_filename(f.filename)
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in current_app.config["ALLOWED_UPLOAD_EXTENSIONS"]:
        flash("Unsupported file type. Upload a PDF, JPG or PNG.", "error")
        return redirect(url_for("documents.expenses", doc_id=doc_id))

    head = f.stream.read(8)
    f.stream.seek(0)
    if not _sniff_ok(ext, head):
        flash("That file does not look like a valid %s file." % ext.lstrip("."), "error")
        return redirect(url_for("documents.expenses", doc_id=doc_id))

    stored = uuid.uuid4().hex + ext
    dest_dir = _dest_dir(doc_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, stored)
    f.save(dest_path)
    size = os.path.getsize(dest_path)

    db.execute("INSERT INTO attachments(expense_id, document_id, filename, stored_name, content_type, size_bytes,"
              " uploaded_by) VALUES (?,?,?,?,?,?,?)",
              (expense_id, doc_id, safe_name or "receipt" + ext, stored, f.mimetype or "", size, me()))
    db.execute("UPDATE expenses SET receipt_attached=1 WHERE id=?", (expense_id,))
    audit(me(), "ATTACHMENT_UPLOAD", "document:%d" % doc_id, stored)
    db.commit()
    flash("Receipt uploaded.", "success")
    return redirect(url_for("documents.expenses", doc_id=doc_id))


@bp.route("/<int:doc_id>/attachments/<int:attachment_id>")
def download_attachment(doc_id, attachment_id):
    get_doc(doc_id)  # enforces can_view, aborts 403/404
    a = get_db().execute("SELECT * FROM attachments WHERE id=? AND document_id=?",
                         (attachment_id, doc_id)).fetchone()
    if a is None:
        abort(404)
    return send_from_directory(_dest_dir(doc_id), a["stored_name"], as_attachment=False,
                               download_name=a["filename"])


@bp.route("/<int:doc_id>/attachments/<int:attachment_id>/delete", methods=["POST"])
def delete_attachment(doc_id, attachment_id):
    doc = get_doc(doc_id)
    if not can_edit(doc):
        abort(403)
    db = get_db()
    a = db.execute("SELECT * FROM attachments WHERE id=? AND document_id=?", (attachment_id, doc_id)).fetchone()
    if a is None:
        abort(404)
    db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
    dest_path = os.path.join(_dest_dir(doc_id), a["stored_name"])
    if os.path.exists(dest_path):
        os.remove(dest_path)
    remaining = db.execute("SELECT COUNT(*) c FROM attachments WHERE expense_id=?",
                           (a["expense_id"],)).fetchone()["c"]
    if remaining == 0:
        db.execute("UPDATE expenses SET receipt_attached=0 WHERE id=?", (a["expense_id"],))
    audit(me(), "ATTACHMENT_DELETE", "document:%d" % doc_id, a["stored_name"])
    db.commit()
    flash("Receipt removed.", "success")
    return redirect(url_for("documents.expenses", doc_id=doc_id))
