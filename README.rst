=======
flipflop
========

FastCGI/WSGI gateway.

`github.com/Kozea/flipflop <https://github.com/Kozea/flipflop>`_

This module is a simplified fork of flup, written by Allan Saddi. It only has
the FastCGI part of the original module.

.. code-block:: python

    #!/usr/bin/env python
    from myapplication import app
    from flipflop import WSGIServer


    WSGIServer(app).run()


*flipflop* is released under the BSD 2-Clause license.
